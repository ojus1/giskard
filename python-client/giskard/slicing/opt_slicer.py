import pandas as pd

from .filters import SignificanceFilter
from .slice import DataSlice, GreaterThan, LowerThan, Query, QueryBasedSliceFunction

from .base import BaseSlicer


class OptSlicer(BaseSlicer):
    def find_slices(self, features, target=None):
        try:
            from optbinning import ContinuousOptimalBinning
        except ImportError as err:
            raise ImportError(
                "Please install the optional package `giskard[scan]` to use this functionality."
                "You can do this by running `pip install giskard[scan]` in your environment."
            ) from err

        target = target or self.target

        if len(features) > 1:
            raise NotImplementedError("Only single-feature slicing is implemented for now.")

        feature = features[0]

        optb = ContinuousOptimalBinning(
            name=feature,
            min_prebin_size=0.02,  # 2% of dataset
            max_pvalue=1e-3,
            max_pvalue_policy="all",
            monotonic_trend=None,
            # class_weight="balanced",
            time_limit=3
            # min_mean_diff=0.01,
        )

        data = self.dataset.df
        optb.fit(data[feature], data[target])
        slice_candidates = make_slices_from_splits(data, optb.splits, features)

        return slice_candidates


def make_slices_from_splits(data: pd.DataFrame, splits: list[float], feature_names: str):
    """Builds data slices from a list of split values."""
    slices = []
    splits = list(splits)
    for left, right in zip([None] + splits, splits + [None]):
        clauses = []
        if left is not None:
            clauses.append(GreaterThan(feature_names[0], left))
        if right is not None:
            clauses.append(LowerThan(feature_names[0], right))

        slices.append(QueryBasedSliceFunction(Query(clauses, False)))

    return slices
