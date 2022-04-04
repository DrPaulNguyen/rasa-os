"""
exp_0_stratified_exclusion
---------------------------
- avoid whatever test data splitting is doing with responses and just use
  sklearns stratified shuffle split
"""

import asyncio
import shutil
import subprocess
from pathlib import Path
import random
from typing import Tuple, Optional, List
from dataclasses import dataclass
import logging
import os

import numpy as np
from sklearn.model_selection import StratifiedShuffleSplit
from tqdm import tqdm
import hydra
from hydra.core.config_store import ConfigStore
from omegaconf import OmegaConf

import rasa.cli.utils as rasa_cli_utils
import rasa.shared.utils.io as rasa_io_utils
from rasa.core.agent import Agent
from rasa.nlu.test import drop_intents_below_freq, run_evaluation
import rasa.model_training as model_training
from rasa.shared.constants import DEFAULT_DATA_PATH, CONFIG_SCHEMA_FILE
from rasa.shared.importers.importer import TrainingDataImporter
from rasa.shared.utils import validation
from rasa.shared.nlu.training_data.training_data import TrainingData

from leaderboard.nlu.base.base_experiment import (
    absolute_path,
    BaseExperiment,
    ExperimentConfiguration,
)
from leaderboard.utils import hydra_utils, rasa_utils

logger = logging.getLogger(__file__)


@dataclass
class IntentExperimentConfiguration(ExperimentConfiguration):
    drop_intents_with_less_than: int = 5
    exclusion_percentage: float = 0.0
    test_fraction: float = 0.2
    random_seed: int = 42


ConfigStore.instance().store(name="config", node=IntentExperimentConfiguration)


@dataclass
class IntentExperiment(BaseExperiment):
    """ntent Classifier Experiment."""

    out_dir: Path
    config: IntentExperimentConfiguration

    def __post_init__(self) -> None:
        if not self.out_dir.is_dir():
            raise ValueError(f"Working directory {self.out_dir} does not exist.")

        # run Rasa's configuration validation
        validation.validate_yaml_schema(
            yaml_file_content=rasa_io_utils.read_file(
                absolute_path(self.config.model.config_path)
            ),
            schema_path=CONFIG_SCHEMA_FILE,
        )
        self.rng = random.Random(self.config.random_seed)

    def get_description(self):
        """Return a description that will be logged as reminder what this is about."""
        return (
            "Experiment where we \n"
            "1. exclude all intents with less than a fixed number of "
            "examples and \n"
            "2. create train and test via sklearn stratified shuffle split using the "
            "messages intents\n"
            "3. during training also remove a fixed percentage of the "
            "training examples before training using the same method as in 2."
        )

    @classmethod
    def load_data(cls, data_path: Path, domain_path: Path) -> TrainingData:
        """Load data from disk."""
        data_path = rasa_cli_utils.get_validated_path(
            absolute_path(data_path), "nlu", DEFAULT_DATA_PATH
        )
        test_data_importer = TrainingDataImporter.load_from_dict(
            training_data_paths=[str(data_path)], domain_path=domain_path
        )
        nlu_data = test_data_importer.get_nlu_data()
        return nlu_data

    def preprocess_data(self, nlu_data: TrainingData) -> TrainingData:
        """Preprocessing applied to **all** data points."""
        nlu_data = drop_intents_below_freq(
            nlu_data, cutoff=self.config.drop_intents_with_less_than
        )
        return nlu_data

    def split_data(
        self, nlu_data: TrainingData, test_fraction: float
    ) -> Tuple[TrainingData, TrainingData]:
        """Split given data into train and test."""

        data_messages = nlu_data.intent_examples
        data_indices = np.arange(len(data_messages))
        labels_str = [message.get("intent") for message in data_messages]

        sss = StratifiedShuffleSplit(
            n_splits=1,
            test_size=test_fraction,
            random_state=self.config.random_seed,
        )
        indices_per_split = next(sss.split(data_indices, labels_str))

        splits = []
        for split_name, indices in zip(["train", "test"], indices_per_split):
            split_messages = [data_messages[data_indices[idx]] for idx in indices]
            split_responses = nlu_data._needed_responses_for_examples(split_messages)
            split_data = TrainingData(
                split_messages,
                entity_synonyms=nlu_data.entity_synonyms,
                regex_features=nlu_data.regex_features,
                lookup_tables=nlu_data.lookup_tables,
                responses=split_responses,
            )
            splits.append(split_data)

        return splits

    def load_splits(self) -> Tuple[TrainingData, TrainingData]:
        full_data = self.load_data(
            data_path=absolute_path(self.config.data.data_path),
            domain_path=absolute_path(self.config.data.domain_path),
        )
        full_data = self.preprocess_data(full_data)
        train, test = self.split_data(
            full_data, test_fraction=self.config.test_fraction
        )
        percentage = self.config.exclusion_percentage
        _, train = train.train_test_split(percentage / 100)
        return train, test

    def run(self, train: TrainingData, test: Optional[TrainingData]) -> None:

        # as before, train and test used in the experiment are persisted
        data_path = self.out_dir / "data"
        data_path.mkdir(parents=True)
        report_path = self.out_dir / "report"
        report_path.mkdir()

        # modify and store training data
        train_split_path = data_path / "train.yml"
        rasa_io_utils.write_text_file(train.nlu_as_yaml(), train_split_path)
        # TODO log stats for training data

        # train model
        model_output_path = self.out_dir / "model"
        model_path = model_training.train_nlu(
            config=absolute_path(self.config.model.config_path),
            nlu_data=train_split_path,
            output=model_output_path,
            fixed_model_name=self.config.model.name,
        )

        # extract training meta data
        model_archive = model_output_path / (self.config.model.name + ".tar.gz")
        rasa_utils.extract_metadata(model_archive, report_path)

        # test model
        if test is not None:
            test_split_path = data_path / "test.yml"
            rasa_io_utils.write_text_file(test.nlu_as_yaml(), test_split_path)

            processor = Agent.load(model_path=model_path).processor
            _ = asyncio.run(
                run_evaluation(
                    test_split_path,
                    processor,
                    output_directory=report_path,
                    errors=True,
                )
            )

        if self.config.clear_rasa_cache:
            shutil.rmtree(self.out_dir / ".rasa")


@hydra.main(config_path=None, config_name="config")
def main(config: IntentExperimentConfiguration) -> float:
    """Executes a single experiment and is invoked via command line."""
    out_dir = Path.cwd()  # with multi-run, the cwd is the experiment sub-folder
    experiment = IntentExperiment(out_dir=out_dir, config=config)
    try:
        experiment.execute()
    except Exception as e:
        logger.exception(f"An error {e} occurred during this experiment.")

    return 1.0  # needed for sweepers, replace with some true score for hypopt


def multirun(
    configs: List[IntentExperimentConfiguration],
    out_dir: Path,
    capture: bool = False,
) -> None:
    """Executes multiple experiments and is not usable from command line yet."""

    experiment_pattern = (
        "config:${model.name}"
        ",test:${test_fraction}"
        ",drop:${drop_intents_with_less_than}"
        ",exclude:${exclusion_percentage}"
        ",seed:${random_seed}"
        "__${now:%Y-%m-%d_%H-%M-%S}"
    )
    script_name = os.path.basename(__file__).replace(".py", "")
    full_pattern = os.path.join(
        out_dir,
        f"{script_name}" + "__${data.name}",
        experiment_pattern,
    )

    command = [
        "python",
        os.path.abspath(__file__),
        f"hydra.run.dir='{full_pattern}'",
    ]

    # validate all configs before starting anything...
    for config in configs:
        config.validate()

    captured = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) if capture else {}
    for config in tqdm(configs):
        args = hydra_utils.to_hydra_cli_args(OmegaConf.structured(config))
        result = subprocess.run(
            command + args, **captured
        )
        if result.returncode != 0:
            logger.error(f"Configuration {config} could not be evaluated.")


if __name__ == "__main__":
    main()
