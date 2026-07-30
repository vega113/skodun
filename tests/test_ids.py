"""Review-id generation: the one definition, in a module nothing else imports.

`ids` exists because THREE call sites now mint review ids -- the foreground
pipeline, the store's reservation transaction, and the dispatcher's durable
pre-record failures -- and the store may not import the pipeline. A second
copy of this function would collide on the one property that makes it safe:
uniqueness inside a single process-second.
"""

from __future__ import annotations

import os
import re

from skodun import ids, pipeline

_SHAPE = re.compile(r"^sk_[0-9]{8}T[0-9]{6}Z_[0-9]+_[0-9a-f]{8}$")


def test_the_id_shape_is_stamp_pid_uuid8():
    value = ids.new_review_id()
    assert _SHAPE.fullmatch(value), value
    assert value.split("_")[2] == str(os.getpid())


def test_the_prefix_is_the_callers_and_defaults_to_sk():
    assert ids.new_review_id().startswith("sk_")
    assert ids.new_review_id("bg_").startswith("bg_")
    # Everything after the prefix keeps its shape whatever the prefix is.
    assert _SHAPE.fullmatch("sk_" + ids.new_review_id("x_")[2:])


def test_two_ids_minted_in_the_same_process_second_differ():
    """The uuid component is mandatory, not decoration.

    Second-resolution time plus pid collides for two ids minted in the same
    process-second -- which a multi-ref push does routinely -- and every writer
    upserts by id, so a collision would silently overwrite a live reservation.
    """
    minted = {ids.new_review_id() for _ in range(200)}
    assert len(minted) == 200


def test_the_pipeline_helper_is_this_function_not_a_second_copy():
    """`pipeline._new_id` is an IMPORT of `ids.new_review_id`.

    Two definitions could drift, and the one that drifted would be the one the
    reservation transaction did not use.
    """
    assert pipeline._new_id is ids.new_review_id
