from __future__ import annotations

import pathlib

# from functools import partial
from itertools import compress
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Union

import cytoolz as tz
import mosaic
import numpy as np
from mosaic.provenance import capture_provenance

from robustnessgym.core.constants import (
    ATTACK,
    CURATION,
    GENERIC,
    SLICEBUILDERS,
    SUBPOPULATION,
    TRANSFORMATION,
)
from robustnessgym.core.identifier import Identifier
from robustnessgym.core.slice import SliceDataPanel as DataPanel
from robustnessgym.core.storage import StorageMixin
from robustnessgym.core.tools import strings_as_json


class SliceBuilder(StorageMixin):
    """Base class for builders that output slices."""

    # Path to a log directory
    logdir: pathlib.Path = pathlib.Path.home() / "robustnessgym" / SLICEBUILDERS

    # Create the log directory
    logdir.mkdir(parents=True, exist_ok=True)

    CATEGORIES = [
        GENERIC,
        SUBPOPULATION,
        ATTACK,
        TRANSFORMATION,
        CURATION,
    ]

    def __init__(
        self,
        identifiers: List[Identifier],
        category: str = GENERIC,
        apply_fn: Callable = None,
        *args,
        **kwargs,
    ):

        super(SliceBuilder, self).__init__(*args, **kwargs)

        # The SliceBuilder belongs to a category
        assert (
            category in self.CATEGORIES
        ), f"argument category must be one of {self.CATEGORIES}"
        self.category = category

        # Each identifier corresponds to a Slice generated by this SliceBuilder
        self.identifiers = identifiers

        # Keep track of the Operation dependencies
        self.prerequisites = (
            set() if "prerequisites" not in kwargs else kwargs["prerequisites"]
        )

        if apply_fn:
            # Assign to the method
            self.apply = apply_fn

    def __repr__(self):
        return (
            f"{self.category}[{self.__class__.__name__}(num_slices={self.num_slices})]"
        )

    @property
    def num_slices(self):
        return len(self.identifiers)

    def __getitem__(self, item: int):
        return self.identifiers[item]

    def __iter__(self):
        yield from self.identifiers

    @capture_provenance(capture_args=["self", "dp", "columns", "batch_size"])
    def __call__(
        self,
        dp: Optional[DataPanel],
        columns: List[str],
        batch_size: int = 100,
        num_proc: int = None,
        *args,
        **kwargs,
    ):

        # Check that prerequisites are satisfied
        self.prerequisites_handler(dp)

        if isinstance(dp, DataPanel):
            # Prepare the data
            self.prepare_dataset(
                dp=dp,
                columns=columns,
                batch_size=batch_size,
                *args,
                **kwargs,
            )

            # Slice a dataset
            slices, slice_membership = self.process_dataset(
                dp=dp,
                columns=columns,
                num_proc=num_proc,
                *args,
                **kwargs,
            )

            return slices, slice_membership

        else:
            return self(
                dp=DataPanel(dp),
                columns=columns,
                *args,
                **kwargs,
            )

    def prerequisites_handler(
        self,
        dp: DataPanel,
    ):
        """Check if the DataPanel satisfies necessary prerequisites in order to
        run the SliceBuilder."""
        # Check if prerequisites are satisfied
        # TODO(karan): move to a method
        pending = {
            prerequisite
            for prerequisite in self.prerequisites
            if not prerequisite.available(dp)
        }

        # TODO(karan): Automatically run the pending pre-requisites
        if pending:
            raise RuntimeError(
                f"Cannot run SliceBuilder, prerequisites {pending} not satisfied."
            )

    def prepare_batch(
        self,
        batch: DataPanel,
        columns: List[str],
        *args,
        **kwargs,
    ) -> None:
        """Apply a preparation function to a batch. Use this to update
        attributes of `self`.

        Args:
            batch: batch of data
            columns: list of columns
            *args: optional additional arguments
            **kwargs: optional additional keyword arguments
        """
        raise NotImplementedError("Implement `prepare_batch`.")

    def _filter_prerequisite_columns(
        self,
        columns: List[str],
        all_columns: List[str],
    ) -> List[str]:
        # Simple filtering that doesn't use columns
        # TODO(karan): improve this by using `columns` to filter further
        return [
            col
            for col in all_columns
            if any(
                [
                    col.startswith(
                        prereq.__name__ if isinstance(prereq, type) else str(prereq)
                    )
                    for prereq in self.prerequisites
                ]
            )
        ]

    def prepare_dataset(
        self,
        dp: DataPanel,
        columns: List[str],
        batch_size: int = 32,
        *args,
        **kwargs,
    ) -> None:
        """Apply a preparation function to the data. Use this to update
        attributes of `self`.

        Args:
            dp: DataPanel
            columns: list of columns
            batch_size: batch size for preparation
            *args: optional additional arguments
            **kwargs: optional additional keyword arguments
        """
        # Set the data format
        with dp.format(
            columns + self._filter_prerequisite_columns(columns, dp.column_names)
        ):
            # Batch the dataset, and prepare each batch
            for batch in dp.batch(batch_size):
                try:
                    # Check if the `prepare_batch` function has been implemented
                    self.prepare_batch(
                        batch=batch,
                        columns=columns,
                        *args,
                        **kwargs,
                    )
                except NotImplementedError:
                    break

    def process_batch(
        self,
        dp: DataPanel,
        columns: List[str],
        *args,
        **kwargs,
    ) -> Tuple[Optional[List[DataPanel]], Optional[np.ndarray]]:
        """Apply a SliceBuilder to a batch of data.

        Args:
            dp: a DataPanel of data
            columns: list of columns
            *args: optional additional arguments
            **kwargs: optional additional keyword arguments

        Returns: tuple of
        (list of slices (as batches), matrix of (example, slice) membership))
        """
        return [dp], None

    def process_dataset(
        self,
        dp: DataPanel,
        columns: List[str],
        batch_size: int = 32,
        num_proc: int = None,
        *args,
        **kwargs,
    ) -> Tuple[List[DataPanel], np.ndarray]:
        """Apply a SliceBuilder to a dataset.

        Args:
            dp: DataPanel
            columns: list of columns
            batch_size: integer batch size
            num_proc: num processes for multiprocessing
            *args: optional additional arguments
            **kwargs: optional additional keyword arguments

        Returns: tuple of (DataPanel, list of Slices,
        matrix of (example, slice) membership)
        """
        # Create slices
        slices = [[DataPanel()] for _ in range(len(self.identifiers))]
        all_slice_memberships = []

        # Batch the dataset, and process each batch
        for batch in dp.batch(batch_size):
            # Process the batch
            sliced_batches, slice_memberships = self.process_batch(
                dp=batch,
                columns=columns,
                *args,
                **kwargs,
            )

            print(sliced_batches, slice_memberships)

            # Incrementally build the slices
            for sl, sl_batch in zip(slices, sliced_batches):
                sl.append(sl_batch)

            # Keep track of the slice memberships
            all_slice_memberships.append(slice_memberships)

        # Create a single slice label matrix
        slice_membership = np.concatenate(all_slice_memberships, axis=0)

        # Create a single DataPanel for each slice
        slices = [mosaic.concat(e, axis=0) for e in slices]

        # TODO(karan): DataPanel doesn't support this
        for i, sl in enumerate(slices):
            # Set the Slice category using the SliceBuilder's category
            sl.category = self.category

            # Append the the lineage
            sl.add_to_lineage(
                category=str(self.category.capitalize()),
                identifier=self.identifiers[i],
                columns=strings_as_json(columns),
            )

        return slices, slice_membership

    def apply(self, *args, **kwargs):
        raise NotImplementedError("Must implement apply.")

    @classmethod
    def join(cls, *slicebuilders: SliceBuilder) -> Sequence[SliceBuilder]:
        """Join many SliceBuilders.

        By default, just returns them.
        """
        return slicebuilders

    @staticmethod
    def filter_batch_by_slice_membership(
        batch: Dict[str, List],
        slice_membership: np.ndarray,
    ) -> List[Dict[str, List]]:
        """Use a matrix of slice membership labels to select the subset of
        examples in each slice.

        Returns a list. Each element in the list corresponds to a single
        slice, and contains the subset of examples in 'batch' that lies
        in that slice.
        """
        return [
            tz.valmap(lambda v: list(compress(v, s)), batch) for s in slice_membership.T
        ]

    @classmethod
    def retrieve(
        cls,
        batch: DataPanel,
        columns: Union[List[str], List[List[str]]],
        proc_fns: Union[str, Callable, List[Union[str, Callable]]] = None,
        identifier: Union[str, Identifier] = None,
        reapply: bool = False,
        **kwargs,
    ) -> Optional[Union[DataPanel, List[DataPanel]]]:
        if not reapply:
            if "slices" not in batch:
                return None

            # Infer the most relevant key to retrieve if an identifier is not specified
            if not identifier:
                for ident_key in batch["slices"][0].keys():
                    # Pick the first key that matches the cls name
                    if ident_key.startswith(cls.__name__):
                        identifier = ident_key
                        break

            try:
                if isinstance(columns[0], str):
                    retrieval = {
                        strings_as_json(columns): [
                            cls.decode(cache[str(identifier)][strings_as_json(columns)])
                            for cache in batch["cache"]
                        ]
                    }
                else:
                    retrieval = {
                        strings_as_json(cols_): [
                            cls.decode(cache[str(identifier)][strings_as_json(cols_)])
                            for cache in batch["cache"]
                        ]
                        for cols_ in columns
                    }
            except KeyError:
                raise ValueError("Could not retrieve information for all keys.")

            # Check if the retrieved information needs to be processed
            if not proc_fns:
                return retrieval
            pass
        else:
            pass
