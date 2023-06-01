import logging
import posixpath
import tempfile
import uuid
from enum import Enum
from pathlib import Path
from typing import Dict, Optional, List, Hashable, Union

import pandas as pd
import yaml
from pandas.api.types import is_numeric_dtype
from zstandard import ZstdDecompressor

from giskard.client.giskard_client import GiskardClient
from giskard.client.io_utils import save_df, compress
from giskard.client.python_utils import warning
from giskard.core.core import DatasetMeta, SupportedColumnTypes
from giskard.core.validation import configured_validate_arguments
from giskard.ml_worker.testing.registry.slicing_function import SlicingFunction
from giskard.ml_worker.testing.registry.transformation_function import TransformationFunction
from giskard.settings import settings

logger = logging.getLogger(__name__)


class Nuniques(Enum):
    CATEGORY = 2
    NUMERIC = 100
    TEXT = 1000


class DataProcessor:
    """
    A class for processing tabular data using a pipeline of functions.

    The pipeline consists of slicing functions that extract subsets of the data and transformation functions that modify it.
    Slicing functions should take a pandas DataFrame as input and return a DataFrame, while transformation functions
    should take a DataFrame and return a modified version of it.

    Attributes:
        pipeline (List[Union[SlicingFunction, TransformationFunction]]): a list of functions to be applied to the data,
            in the order in which they were added.

    Methods:
        add_step(processor: Union[SlicingFunction, TransformationFunction]) -> DataProcessor:
            Add a function to the processing pipeline, if it is not already the last step in the pipeline. Return self.

        apply(dataset: Dataset, apply_only_last=False) -> Dataset:
            Apply the processing pipeline to the given dataset. If apply_only_last is True, apply only the last function
            in the pipeline. Return a new Dataset object containing the processed data.

        __repr__() -> str:
            Return a string representation of the DataProcessor object, showing the number of steps in its pipeline.
    """
    pipeline: List[Union[SlicingFunction, TransformationFunction]] = []

    @configured_validate_arguments
    def add_step(self, processor: Union[SlicingFunction, TransformationFunction]):
        if not len(self.pipeline) or self.pipeline[-1] != processor:
            self.pipeline.append(processor)
        return self

    def apply(self, dataset: 'Dataset', apply_only_last=False):
        df = dataset.df.copy()

        while len(self.pipeline):
            step = self.pipeline.pop(-1 if apply_only_last else 0)

            df = step(df)

            if apply_only_last:
                break

        if df.empty:
            raise ValueError("Processing pipeline produced an empty dataset")

        ret = Dataset(df=df,
                      name=dataset.name,
                      target=dataset.target,
                      cat_columns=dataset.cat_columns,
                      column_types=dataset.column_types)

        if len(self.pipeline):
            ret.data_processor = self
        return ret

    def __repr__(self) -> str:
        return f"DataProcessor: {len(self.pipeline)} steps"


class Dataset:
    """
    A class that represents a dataset object.

    Members:
        name (str):
            The name of the dataset.
        target (str):
            The name of the target column in the dataset.
        column_types (Dict[str, str]):
            A dictionary mapping column names to their corresponding column types.
        df (pd.DataFrame):
            The pandas DataFrame containing the data.
        id (uuid.UUID):
            The unique identifier of the dataset.
        data_processor (DataProcessor):
            A `DataProcessor` object that can be used to transform the dataset.

    Methods:
        __init__(self, df: pd.DataFrame, name: Optional[str] = None, target: Optional[str] = None,
        cat_columns: Optional[List[str]] = None, infer_column_types: Optional[bool] = False,
        column_types: Optional[Dict[str, str]] = None, id: Optional[uuid.UUID] = None) -> None:
            Constructs a new `Dataset` object.
        add_slicing_function(self, slicing_function: SlicingFunction):
            Adds a slicing function to the dataset's `DataProcessor`.
        add_transformation_function(self, slicing_function: SlicingFunction):
            Adds a transformation function to the dataset's `DataProcessor`.
        slice(self, slicing_function: Optional[SlicingFunction] = None):
            Applies the slicing function to the dataset's `DataProcessor`.
        transform(self, transformation_function: TransformationFunction):
            Applies the transformation function to the dataset's `DataProcessor`.
        process(self):
            Applies all the functions in the dataset's `DataProcessor`.
        check_hashability(df):
            Checks if the DataFrame is hashable.
        infer_column_types(df, column_dtypes, no_cat=False):
            Infers the column types of a DataFrame.
        extract_column_types(column_dtypes, cat_columns):
            Extracts the column types from a DataFrame.
        extract_column_dtypes(df):
            Extracts the column data types from a DataFrame.
        upload(self, client: GiskardClient, project_key: str):
            Uploads the dataset to the Giskard server.
        meta(self):
            Returns a `DatasetMeta` object containing metadata about the dataset.
    """
    name: str
    target: str
    column_types: Dict[str, str]
    df: pd.DataFrame
    id: uuid.UUID
    data_processor: DataProcessor = DataProcessor()

    @configured_validate_arguments
    def __init__(
            self,
            df: pd.DataFrame,
            name: Optional[str] = None,
            target: Optional[str] = None,
            cat_columns: Optional[List[str]] = None,
            infer_column_types: Optional[bool] = False,
            column_types: Optional[Dict[str, str]] = None,
            id: Optional[uuid.UUID] = None
    ) -> None:
        if df.empty:
            raise ValueError("Please provide a non-empty df to construct the Dataset object.")
        if id is None:
            self.id = uuid.uuid4()
        else:
            self.id = id
        self.name = name
        self.df = pd.DataFrame(df)
        self.target = target
        if not self.target:
            warning(
                "You did not provide the optional argument 'target'. "
                "'target' is the column name in df corresponding to the actual target variable (ground truth).")
        self.check_hashability(self.df)
        self.column_dtypes = self.extract_column_dtypes(self.df)
        if column_types:
            self.column_types = column_types
        elif cat_columns:
            if not set(cat_columns).issubset(list(df.columns)):
                raise ValueError("The provided 'cat_columns' are not all part of your dataset 'columns'. "
                                 "Please make sure that `cat_columns` refers to existing columns in your dataset.")
            self.column_types = self.extract_column_types(self.column_dtypes, cat_columns)
        elif infer_column_types:
            self.column_types = self.infer_column_types(self.df, self.column_dtypes)
        else:
            self.column_types = self.infer_column_types(self.df, self.column_dtypes, no_cat=True)
            warning(
                "You did not provide any of [column_types, cat_columns, infer_column_types = True] for your Dataset. "
                "In this case, we assume that there's no categorical columns in your Dataset.")

        from giskard.core.dataset_validation import validate_numeric_columns
        validate_numeric_columns(self)

    def add_slicing_function(self, slicing_function: SlicingFunction):
        self.data_processor.add_step(slicing_function)
        return self

    def add_transformation_function(self, slicing_function: SlicingFunction):
        self.data_processor.add_step(slicing_function)
        return self

    def slice(self, slicing_function: Optional[SlicingFunction] = None):
        if slicing_function:
            return self.data_processor.add_step(slicing_function).apply(self, apply_only_last=True)
        else:
            return self

    @configured_validate_arguments
    def transform(self, transformation_function: TransformationFunction):
        return self.data_processor.add_step(transformation_function).apply(self, apply_only_last=True)

    def process(self):
        return self.data_processor.apply(self)

    @staticmethod
    def check_hashability(df):
        df_objects = df.select_dtypes(include='object')
        non_hashable_cols = []
        for col in df_objects.columns:
            if not isinstance(df[col].iat[0], Hashable):
                non_hashable_cols.append(col)

        if non_hashable_cols:
            raise TypeError(
                f"The following columns in your df: {non_hashable_cols} are not hashable. "
                f"We currently support only hashable column types such as int, bool, str, tuple and not list or dict.")

    @staticmethod
    def infer_column_types(df, column_dtypes, no_cat=False):
        # TODO: improve this method
        nuniques = df.nunique()
        column_types = {}
        for col, col_type in column_dtypes.items():
            if nuniques[col] <= Nuniques.CATEGORY.value and not no_cat:
                column_types[col] = SupportedColumnTypes.CATEGORY.value
            elif is_numeric_dtype(df[col]):
                column_types[col] = SupportedColumnTypes.NUMERIC.value
            else:
                column_types[col] = SupportedColumnTypes.TEXT.value
        return column_types

    @staticmethod
    def extract_column_types(column_dtypes, cat_columns):
        column_types = {}
        for cat_col in cat_columns:
            column_types[cat_col] = SupportedColumnTypes.CATEGORY.value
        for col, col_type in column_dtypes.items():
            if col not in cat_columns:
                column_types[col] = SupportedColumnTypes.NUMERIC.value if is_numeric_dtype(
                    col_type) else SupportedColumnTypes.TEXT.value

        return column_types

    @staticmethod
    def extract_column_dtypes(df):
        return df.dtypes.apply(lambda x: x.name).to_dict()

    def upload(self, client: GiskardClient, project_key: str):
        from giskard.core.dataset_validation import validate_dataset

        validate_dataset(self)

        dataset_id = str(self.id)

        with tempfile.TemporaryDirectory(prefix="giskard-dataset-") as local_path:
            original_size_bytes, compressed_size_bytes = self.save(Path(local_path), dataset_id)
            client.log_artifacts(local_path, posixpath.join(project_key, "datasets", dataset_id))
            client.save_dataset_meta(
                project_key,
                dataset_id,
                self.meta,
                original_size_bytes=original_size_bytes,
                compressed_size_bytes=compressed_size_bytes,
            )
        return dataset_id

    @property
    def meta(self):
        return DatasetMeta(
            name=self.name, target=self.target, column_types=self.column_types, column_dtypes=self.column_dtypes
        )

    @staticmethod
    def cast_column_to_dtypes(df, column_dtypes):
        current_types = df.dtypes.apply(lambda x: x.name).to_dict()
        logger.info(f"Casting dataframe columns from {current_types} to {column_dtypes}")
        if column_dtypes:
            try:
                df = df.astype(column_dtypes)
            except Exception as e:
                raise ValueError("Failed to apply column types to dataset") from e
        return df

    @classmethod
    def load(cls, local_path: str):
        with open(local_path, "rb") as ds_stream:
            return pd.read_csv(
                ZstdDecompressor().stream_reader(ds_stream),
                keep_default_na=False,
                na_values=["_GSK_NA_"],
            )

    @classmethod
    def download(cls, client: GiskardClient, project_key, dataset_id):
        local_dir = settings.home_dir / settings.cache_dir / project_key / "datasets" / dataset_id

        if client is None:
            # internal worker case, no token based http client
            assert local_dir.exists(), f"Cannot find existing dataset {project_key}.{dataset_id}"
            with open(Path(local_dir) / "giskard-dataset-meta.yaml") as f:
                saved_meta = yaml.load(f, Loader=yaml.Loader)
                meta = DatasetMeta(
                    name=saved_meta["name"],
                    target=saved_meta["target"],
                    column_types=saved_meta["column_types"],
                    column_dtypes=saved_meta["column_dtypes"],
                )
        else:
            client.load_artifact(local_dir, posixpath.join(project_key, "datasets", dataset_id))
            meta: DatasetMeta = client.load_dataset_meta(project_key, dataset_id)

        df = cls.load(local_dir / "data.csv.zst")
        df = cls.cast_column_to_dtypes(df, meta.column_dtypes)
        return cls(df=df, name=meta.name, target=meta.target, column_types=meta.column_types, id=uuid.UUID(dataset_id))

    @staticmethod
    def _cat_columns(meta):
        return [fname for (fname, ftype) in meta.column_types.items() if
                ftype == SupportedColumnTypes.CATEGORY] if meta.column_types else None

    @property
    def cat_columns(self):
        return self._cat_columns(self.meta)

    def save(self, local_path: Path, dataset_id):
        with open(local_path / "data.csv.zst", "wb") as f:
            uncompressed_bytes = save_df(self.df)
            compressed_bytes = compress(uncompressed_bytes)
            f.write(compressed_bytes)
            original_size_bytes, compressed_size_bytes = len(uncompressed_bytes), len(compressed_bytes)

            with open(Path(local_path) / "giskard-dataset-meta.yaml", "w") as meta_f:
                yaml.dump(
                    {
                        "id": dataset_id,
                        "name": self.meta.name,
                        "target": self.meta.target,
                        "column_types": self.meta.column_types,
                        "column_dtypes": self.meta.column_dtypes,
                        "original_size_bytes": original_size_bytes,
                        "compressed_size_bytes": compressed_size_bytes,
                    },
                    meta_f,
                    default_flow_style=False,
                )
            return original_size_bytes, compressed_size_bytes

    @property
    def columns(self):
        return self.df.columns

    def __len__(self):
        return len(self.df)
