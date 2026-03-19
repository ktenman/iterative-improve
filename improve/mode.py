from enum import Enum


class Mode(Enum):
    SEQUENTIAL = "sequential"
    BATCH = "batch"
    PARALLEL = "parallel"
