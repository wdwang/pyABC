import datetime
import os
from typing import List, Tuple

import git
import numpy as np
import pandas as pd
import scipy as sp
from sqlalchemy import func
from itertools import groupby

from ..parameters import ValidParticle
from .db_model import ABCSMC, Population, Model, Particle, Parameter, Sample, SummaryStatistic, Base
from functools import wraps

import logging
history_logger = logging.getLogger("History")


def with_session(f):
    @wraps(f)
    def f_wrapper(self: "History", *args, **kwargs):
        history_logger.info('Database access through "{}"'.format(f.__name__))
        no_session = self._session is None and self._engine is None
        if no_session:
            self._make_session()
        res = f(self, *args, **kwargs)
        if no_session:
            self._close_session()
        return res
    return f_wrapper


class History:
    """
    History for ABCSMC.

    This class records the evolution of the populations and stores the ABCSMC results.

    Parameters
    ----------
    db_path: stt
        SQLAlchemy database identifier.

    nr_models: int
        Nr of models.

    model_names: List[str]
        List of model names.

    min_nr_particles_per_population: int
        Minimum nr of particles per population.

    debug: bool
        Whether to print additional debug output.




    .. warning::

        Most likely you will never have to instantiate the class yourself.
        An instance of this class is returned by the ``ABCSMC.run`` method.
        It can then be used for querying. However, most likely even that won't be
        used as querying is usually done on the stored database usind the abc_loader.

    """
    def __init__(self, db_path: str, nr_models: int, model_names: List[str], debug=False):
        self.nr_models = nr_models
        "Only counts the simulations which appear in particles. If a simulation terminated prematurely it is not counted."
        self.db_path = db_path
        self.model_names = list(model_names)
        self._session = None
        self._engine = None

    @with_session
    def weighted_parameters_dataframe(self, t, m):
        if t is None:
            t = self.max_t

        query = (self._session.query(Particle.id, Parameter.name, Parameter.value, Particle.w)
                 .filter(Particle.id == Parameter.particle_id)
                 .join(Model).join(Population)
                 .filter(Model.m == m)
                 .filter(Population.t == t)
                 .join(ABCSMC)
                 .filter(ABCSMC.id == self.id))
        df = pd.read_sql_query(query.statement, self._engine)
        pars = df.pivot("id", "name", "value").sort_index()
        w = df[["id", "w"]].drop_duplicates().set_index("id").sort_index()
        w_arr = w.w.as_matrix()
        assert w_arr.size == 0 or np.isclose(w_arr.sum(), 1), "weight not close to 1, w.sum()={}".format(w_arr.sum())
        return pars, w_arr

    @with_session
    def get_all_populations(self):
        query = (self._session.query(Population.t, Population.population_end_time,
                                     Population.nr_samples, Population.epsilon)
                 .filter(Population.abc_smc_id == self.id))
        df = pd.read_sql_query(query.statement, self._engine)
        return df

    @with_session
    def store_initial_data(self, ground_truth_model: int, options,
                           observed_summary_statistics: dict,
                           ground_truth_parameter: dict,
                           distance_function_json_str: str,
                           eps_function_json_str: str):
        """
        Store the initial configuration data.

        Parameters
        ----------
        ground_truth_model: int
            Nr of the ground truth model.

        observed_summary_statistics: dict
            the measured summary statistics

        ground_truth_parameter: dict
            the ground truth parameters

        distance_function_json_str: str
            the distance function represented as json string

        eps_function_json_str: str
            the epsilon represented as json string
        """
        # store ground truth to db
        try:
            git_hash = git.Repo(os.environ['PYTHONPATH']).head.commit.hexsha
        except (git.exc.NoSuchPathError, KeyError) as e:
            git_hash = str(e)
        abc_smc_simulation = ABCSMC(json_parameters=str(options),
                                    start_time=datetime.datetime.now(),
                                    git_hash=git_hash,
                                    distance_function=distance_function_json_str,
                                    epsilon_function=eps_function_json_str)
        population = Population(t=-1, nr_samples=0, epsilon=0)
        abc_smc_simulation.populations.append(population)

        model = Model(m=ground_truth_model, p_model=1, name=self.model_names[ground_truth_model])
        population.models.append(model)

        gt_part = Particle(w=1)
        model.particles.append(gt_part)

        for key, value in ground_truth_parameter.items():
                gt_part.parameters.append(Parameter(name=key, value=value))
        sample = Sample(distance=0)
        gt_part.samples = [sample]
        sample.summary_statistics = [SummaryStatistic(name=key, value=value)
                                     for key, value in observed_summary_statistics.items()]
        self._session.add(abc_smc_simulation)
        self._session.commit()
        self.id = abc_smc_simulation.id
        history_logger.info("Start {}".format(abc_smc_simulation))

    @property
    @with_session
    def total_nr_simulations(self):
        "Total nr of simulations/samples."
        nr_sim = self._session.query(func.sum(Population.nr_samples)).join(ABCSMC).filter(ABCSMC.id == self.id).one()[0]
        return nr_sim

    def _make_session(self):
        # TODO: check if the session creation and closing is still necessary
        # I think I did this funnny construction due to some pickling issues but I'm not quite sure anymore
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        engine = create_engine(self.db_path, connect_args={'timeout': 120})
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        session = Session()
        self._session = session
        self._engine = engine
        return session

    def _close_session(self):
        self._session.close()
        self._engine.dispose()
        self._session = None
        self._engine = None

    @with_session
    def done(self):
        "Close database sessions and store end time of population."
        abc_smc_simulation = (self._session.query(ABCSMC)
                              .filter(ABCSMC.id == self.id)
                              .one())
        abc_smc_simulation.end_time = datetime.datetime.now()
        self._session.commit()
        history_logger.debug("Done {}".format(abc_smc_simulation))

    @with_session
    def _save_to_population_db(self, t: int, current_epsilon: float, nr_simulations:int,
                               store, model_probabilities):
        # sqlalchemy experimental stuff and highly inefficient implementation here
        # but that is ok for testing purposes for the moment
        # prepare
        abc_smc_simulation = (self._session.query(ABCSMC)
                              .filter(ABCSMC.id == self.id)
                              .one())

        # store the population
        population = Population(t=t, nr_samples=nr_simulations, epsilon=current_epsilon)
        abc_smc_simulation.populations.append(population)
        for m in range(len(store)):
            model = Model(m=m, p_model=model_probabilities[m], name=self.model_names[m])
            population.models.append(model)
            for store_item in store[m]:
                weight = store_item['weight']
                distance_list = store_item['distance_list']
                parameter = store_item['parameter']
                summary_statistics_list = store_item['summary_statistics_list']
                particle = Particle(w=weight)
                model.particles.append(particle)
                for key, value in parameter.items():
                    if isinstance(value, dict):
                        for key_dict, value_dict in value.items():
                            particle.parameters.append(Parameter(name=key + "_" + key_dict, value=value_dict))
                    else:
                        particle.parameters.append(Parameter(name=key, value=value))
                for distance, summ_stat in zip(distance_list, summary_statistics_list):
                    sample = Sample(distance=distance)
                    particle.samples.append(sample)
                    for name, value in summ_stat.items():
                        sample.summary_statistics.append(SummaryStatistic(name=name, value=value))

        self._session.commit()
        history_logger.debug("Appended population")

    def append_population(self, t: int, current_epsilon: float,
                          particle_population: List[ValidParticle], nr_simulations: int):
        """
        Append population to database.

        Parameters
        ----------
        t: int
            Population number.

        current_epsilon: float
            Current epsilon value.

        particle_population: list
            List of sampled particles

        Returns
        -------

        enough_particles: bool
            Whether enough particles were found in the population.

        """
        store, model_probabilities = normalize(particle_population, self.nr_models)
        self._save_to_population_db(t, current_epsilon, nr_simulations, store, model_probabilities)

    def get_results_distribution(self, m: int, parameter: str) -> (np.ndarray, np.ndarray):
        """
        Returns parameter values and weights of the last population.

        Parameters
        ----------

        m: int
            Model number

        parameter: str
            Parameter name.

        Returns
        -------

        results: Tuple[np.ndarray]
            results = (points, weights) with the points and the weights of the last population.
        """
        df, w = self.weighted_parameters_dataframe(None, m)
        return df[parameter].as_matrix(), w

    @with_session
    def get_model_probabilities(self, t=None) -> np.ndarray:
        """
        Model probabilities.

        Parameters
        ----------
        t: int or None
            Population. Defaults to None, i.e. the last population.

        Returns
        -------
        probabilities: np.ndarray
            Model probabilities
        """
        if t is not None and t < 0:
            raise Exception("Model probabilities only for t >= 0 or t = None defined.")
        if t is None:
            t = (self._session
                 .query(func.max(Population.t))
                 .filter(Population.abc_smc_id == self.id)
                 .one()
                 [0])

        p_models = (self._session
                    .query(Model.p_model)
                    .join(Population)
                    .join(ABCSMC)
                    .filter(ABCSMC.id == self.id)
                    .filter(Population.t == t)
                    .order_by(Model.m)
                    .all())

        p_models_arr = sp.array([r[0] for r in p_models], dtype=float)
        return p_models_arr

    def nr_of_models_alive(self, t=None) -> int:
        """
        Number of models still alive.

        Parameters
        ----------
        t: int
            Population number

        Returns
        -------
        nr_alive: int >= 0 or None
            Number of models still alive.
            None is for the last population
        """
        model_probs = self.get_model_probabilities(t)
        return int((model_probs > 0).sum())

    @with_session
    def get_weighted_distances(self, t: int) -> pd.DataFrame:
        """
        Median of a population's distances to the measured sample

        Parameters
        ----------
        t: int
            Population number

        Returns
        -------

        median: float
            The median of the distances.
        """
        if t is None:
            t = self.max_t

        model_probabilities = self.get_model_probabilities(t)
        model_probabilities_df = pd.DataFrame({"m": list(range(len(model_probabilities))),
                                               "model_probabilities": model_probabilities})
        query = (self._session.query(Sample.distance, Particle.w, Model.m)
                 .join(Particle)
                 .join(Model).join(Population).join(ABCSMC)
                 .filter(ABCSMC.id == self.id)
                 .filter(Population.t == t))
        df = pd.read_sql_query(query.statement, self._engine)
        df_weighted = df.merge(model_probabilities_df)
        df_weighted["w"] *= df_weighted["model_probabilities"]
        return df_weighted

    @property
    @with_session
    def max_t(self):
        """
        Current population.
        """
        max_t = self._session.query(func.max(Population.t)).join(ABCSMC).filter(ABCSMC.id == self.id).one()[0]
        return max_t

    @with_session
    def get_sum_stats(self, t, m):
        if t is None:
            t = self.max_t

        particles = (self._session.query(Particle)

         .join(Model).join(Population).join(ABCSMC)
         .filter(ABCSMC.id == self.id)
         .filter(Population.t == t)
         .filter(Model.m == m)
         .all())

        results = []
        weights = []
        for particle in particles:
            for sample in particle.samples:
                weights.append(particle.w)
                sum_stats = {}
                for summary_statistics in sample.summary_statistics:
                    sum_stats[summary_statistics.name] = summary_statistics.value
                results.append(sum_stats)
        return sp.array(weights), results


def normalize(population: List[ValidParticle], nr_models: int):
    """
      * Normalize particle weights according to nr of particles in a model
      * Caclculate marginal model probabilities
    """
    # TODO: This has a medium ugly side effect... maybe it is ok
    population = list(population)

    store = [[] for _ in range(nr_models)]

    for particle in population:
        if particle is not None:  # particle might be none or empty if no particle was found within the allowed nr of sample attempts
            store[particle.m].append(particle)
        else:
            print("ABC History warning: Empty particle.")


    model_total_weights = [sum(particle.weight for particle in model) for model in store]
    population_total_weight = sum(model_total_weights)
    model_probabilities = [w / population_total_weight for w in model_total_weights]

    # normalize within each model
    for model_total_weight, model in zip(model_total_weights, store):
        for particle in model:
            particle.weight /= model_total_weight

    return store, model_probabilities

