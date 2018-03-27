from pyabc.sampler import (MulticoreParticleParallelSampler,
                           MulticoreEvalParallelSampler)
from pyabc.population import FullInfoParticle
import pytest
from multiprocessing import ProcessError


class UnpickleAble:
    def __init__(self):
        self.accepted = True

    def __getstate__(self):
        raise Exception

    def __call__(self, *args, **kwargs):
        return FullInfoParticle([], [], [], [], [], [], True)


unpickleable = UnpickleAble()


@pytest.fixture(params=[MulticoreParticleParallelSampler,
                        MulticoreEvalParallelSampler])
def sampler(request):
    return request.param()


def test_no_pickle(sampler):
    sampler.sample_until_n_accepted(10, unpickleable)


def raise_exception(*args):
    raise Exception("Deliberate exception to be raised in the worker "
                    "processes and to be propagated to the parent process.")


def test_exception_from_worker_propagated(sampler):
    with pytest.raises(ProcessError):
        sampler.sample_until_n_accepted(10, raise_exception)
