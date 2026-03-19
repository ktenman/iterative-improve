from improve.mode import Mode


class TestMode:
    def test_sequential_value_is_sequential(self):
        assert Mode.SEQUENTIAL.value == "sequential"

    def test_batch_value_is_batch(self):
        assert Mode.BATCH.value == "batch"

    def test_parallel_value_is_parallel(self):
        assert Mode.PARALLEL.value == "parallel"

    def test_enum_has_exactly_three_members(self):
        assert len(Mode) == 3
