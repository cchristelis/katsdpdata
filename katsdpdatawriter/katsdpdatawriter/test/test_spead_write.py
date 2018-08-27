from unittest import mock

import numpy as np
from nose.tools import assert_equal, assert_count_equal, assert_is_instance
import asynctest
from aiokatcp import SensorSet
from katdal.chunkstore import ChunkStore

from ..spead_write import Array, RechunkerGroup, io_sensors
from ..rechunk import Offset
from ..bounded_executor import BoundedThreadPoolExecutor


class TestArray:
    def setup(self) -> None:
        self.array = Array(
            'foo',
            in_chunks=((1,), (4, 4, 4), (2, 2)),
            out_chunks=((2,), (2, 2, 4, 4), (2, 2)),
            fill_value=253,
            dtype=np.float32)

    def test_dtype(self) -> None:
        # Check that the converter converted a dtype-like to a real dtype
        assert_equal(self.array.dtype, np.dtype(np.float32))
        assert_is_instance(self.array.dtype, np.dtype)

    def test_substreams(self) -> None:
        assert_equal(self.array.substreams, 6)

    def test_shape(self) -> None:
        assert_equal(self.array.shape, (1, 12, 4))

    def test_nbytes(self) -> None:
        assert_equal(self.array.nbytes, 192)


def _join(*args: str) -> str:
    return '/'.join(args)


class TestRechunkerGroup(asynctest.TestCase):
    def setUp(self) -> None:
        self.chunk_store = mock.create_autospec(spec=ChunkStore, spec_set=True, instance=True)
        self.chunk_store.join = _join

        self.sensors = SensorSet(set())
        for sensor in io_sensors():
            self.sensors.add(sensor)

        self.arrays = [
            Array('weights',
                  ((1,), (4, 4), (2,)),
                  ((1,), (2, 2, 2, 2), (2,)),
                  0, np.uint8),
            Array('weights_channel',
                  ((1,), (4, 4)),
                  ((2,), (2, 2, 2, 2)),
                  0, np.float32)
        ]

        self.weights = np.arange(32).reshape(2, 8, 2).astype(np.uint8)
        self.weights_channel = np.arange(16).reshape(2, 8).astype(np.float32)

        self.executor = BoundedThreadPoolExecutor(4)
        self.r = RechunkerGroup(self.executor, self.chunk_store,
                                self.sensors, 'prefix', self.arrays)

    def tearDown(self):
        self.executor.shutdown(wait=True)

    def add_chunks(self, offset: Offset) -> None:
        slices = np.s_[offset[0]:offset[0]+1, offset[1]:offset[1]+4, :]
        weights = self.weights[slices]
        weights_channel = self.weights_channel[slices[:2]]
        self.r.add(offset, [weights, weights_channel])

    async def test(self) -> None:
        for i in range(0, 8, 4):
            for j in range(2):
                self.add_chunks((j, i))
        chunk_info = await self.r.get_chunk_info()

        expected_calls = []
        for i in range(0, 8, 4):
            for j in range(2):
                for k in range(i, i + 4, 2):
                    expected_calls.append(mock.call(
                        'prefix/weights', np.s_[j:j+1, k:k+2, 0:2], mock.ANY))
        for i in range(0, 8, 2):
            expected_calls.append(mock.call(
                'prefix/weights_channel', np.s_[0:2, i:i+2], mock.ANY))
        assert_count_equal(expected_calls, self.chunk_store.put_chunk.mock_calls)
        # Check the array values. assert_count_equal doesn't work well for this
        # because of how equality operators are implemented in numpy.
        for call in self.chunk_store.put_chunk.mock_calls:
            name, slices, value = call[1]
            if name == 'prefix/weights':
                np.testing.assert_array_equal(self.weights[slices], value)
            else:
                np.testing.assert_array_equal(self.weights_channel[slices], value)

        assert_equal(
            chunk_info,
            {
                'weights': {
                    'prefix': 'prefix',
                    'chunks': ((1, 1), (2, 2, 2, 2), (2,)),
                    'shape': (2, 8, 2),
                    'dtype': np.uint8
                },
                'weights_channel': {
                    'prefix': 'prefix',
                    'chunks': ((2,), (2, 2, 2, 2)),
                    'shape': (2, 8),
                    'dtype': np.float32
                }
            })


# SpeadWriter gets exercised via its derived classes
