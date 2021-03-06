import datetime
import os
from typing import List, Union
import json
import numpy as np
import pandas as pd
import scipy as sp
from sqlalchemy import func
from .db_model import (ABCSMC, Population, Model, Particle,
                       Parameter, Sample, SummaryStatistic, Base)
from functools import wraps
import logging
history_logger = logging.getLogger("History")


def with_session(f):
    @wraps(f)
    def f_wrapper(self: "History", *args, **kwargs):
        history_logger.debug('Database access through "{}"'.format(f.__name__))
        no_session = self._session is None and self._engine is None
        if no_session:
            self._make_session()
        res = f(self, *args, **kwargs)
        if no_session:
            self._close_session()
        return res
    return f_wrapper


def internal_docstring_warning(f):
    first_line = f.__doc__.split("\n")[1]
    indent_level = len(first_line) - len(first_line.lstrip())
    indent = " " * indent_level
    warning = (
        "\n" + indent +
        "**Note.** This function is called by the :class:`pyabc.ABCSMC` "
        "class internally. "
        "You should most likely not find it necessary to call "
        "this method under normal circumstances.")

    f.__doc__ += warning
    return f


def git_hash():
    try:
        import git
    except ImportError:
        return "Install pyABC's optional git dependency for git support"
    try:
        git_hash = git.Repo(os.getcwd()).head.commit.hexsha
    except (git.exc.NoSuchPathError, KeyError,
            git.exc.InvalidGitRepositoryError) as e:
        git_hash = str(e)
    return git_hash


class History:
    """
    History for ABCSMC.

    This class records the evolution of the populations
    and stores the ABCSMC results.

    Parameters
    ----------

    db: str
        SQLAlchemy database identifier.

    """
    DB_TIMEOUT = 120

    def __init__(self, db: str):
        """
        Only counts the simulations which appear in particles.
        If a simulation terminated prematurely, it is not counted.
        """
        self.db_identifier = db
        self._session = None
        self._engine = None
        self.id = self._pre_calculate_id()

    def db_file(self):
        f = self.db_identifier.split(":")[-1][3:]
        return f

    @property
    def inmemory(self):
        return (self._engine is not None
                and str(self._engine.url) == "sqlite://")

    @property
    def db_size(self) -> Union[int, str]:
        """
        Size of the database.

        Returns
        -------

        db_size: int, str
            Size of the SQLite database in MB.
            Currently this only works for SQLite databases.

            Returns an error string if the DB size cannot be calculated.

        """
        try:
            return os.path.getsize(self.db_file()) / 10**6
        except FileNotFoundError:
            return "Cannot calculate size"

    @with_session
    def all_runs(self):
        """
        Get all ABCSMC runs which are stored in the database.
        """
        runs = self._session.query(ABCSMC).all()
        return runs

    @with_session
    def _pre_calculate_id(self):
        abcs = self._session.query(ABCSMC).all()
        if len(abcs) == 1:
            return abcs[0].id
        return None

    @with_session
    def alive_models(self, t) -> List:
        """
        Get the models which are still alive at time `t`.

        Parameters
        ----------

        t: int
            Population nr

        Returns
        -------

        alive: List
            A list which contains the indices of those
            models which are still alive

        """
        t = int(t)
        alive = (self._session.query(Model.m)
                 .join(Population)
                 .join(ABCSMC)
                 .filter(ABCSMC.id == self.id)
                 .filter(Population.t == t)).all()
        return sorted([a[0] for a in alive])

    @with_session
    def get_distribution(self, m: int, t: int=None) \
            -> (pd.DataFrame, np.ndarray):
        """
        Returns the weighted population sample as pandas DataFrame.

        Parameters
        ----------

        m: int
            model index

        t: int, optional
            Population number.
            If t is not specified, then the last population is returned.



        Returns
        -------

        df, w: pandas.DataFrame, np.ndarray

        df:
            is a DataFrame of parameters
        w:
            are the weights associated with each parameter
        """
        m = int(m)
        if t is None:
            t = self.max_t
        else:
            t = int(t)

        query = (self._session.query(Particle.id, Parameter.name,
                                     Parameter.value, Particle.w)
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
        assert w_arr.size == 0 or np.isclose(w_arr.sum(), 1),\
            "weight not close to 1, w.sum()={}".format(w_arr.sum())
        return pars, w_arr

    @with_session
    def model_names(self, t=-1):
        res = (self._session.query(Model.name)
               .join(Population)
               .join(ABCSMC)
               .filter(ABCSMC.id == self.id)
               .filter(Population.t == t)
               .filter(Model.name.isnot(None))
               .order_by(Model.m)
               .distinct().all())
        return [r[0] for r in res]

    @with_session
    def get_abc(self):
        return self._session.query(ABCSMC).filter(ABCSMC.id == self.id).one()

    @with_session
    def get_all_populations(self):
        """
        Returns a pandas DataFrame with columns

        * `t`: Population number
        * `population_end_time`: The end time of the population
        * `samples`: The number of sample attempts performed
           for a population
        * `epsilon`: The acceptance threshold for the population.

        Returns
        -------

        all_populations: pd.DataFrame
            DataFrame with population info
        """
        query = (self._session.query(Population.t,
                                     Population.population_end_time,
                                     Population.nr_samples, Population.epsilon)
                 .filter(Population.abc_smc_id == self.id))
        df = pd.read_sql_query(query.statement, self._engine)
        particles = self.get_nr_particles_per_population()
        particles.index += 1
        df["particles"] = particles
        df = df.rename(columns={"nr_samples": "samples"})
        return df

    @with_session
    @internal_docstring_warning
    def store_initial_data(self, ground_truth_model: int, options: dict,
                           observed_summary_statistics: dict,
                           ground_truth_parameter: dict,
                           model_names: List[str],
                           distance_function_json_str: str,
                           eps_function_json_str: str,
                           population_strategy_json_str: str):
        """
        Store the initial configuration data.

        Parameters
        ----------

        ground_truth_model: int
            Nr of the ground truth model.

        options: dict
            Of ABC metadata

        observed_summary_statistics: dict
            the measured summary statistics

        ground_truth_parameter: dict
            the ground truth parameters

        model_names: List
            A list of model names

        distance_function_json_str: str
            The distance function represented as json string

        eps_function_json_str: str
            The epsilon represented as json string

        population_strategy_json_str: str
            The population strategy represented as json string

        """

        # store ground truth to db

        abcsmc = ABCSMC(
            json_parameters=str(options),
            start_time=datetime.datetime.now(),
            git_hash=git_hash(),
            distance_function=distance_function_json_str,
            epsilon_function=eps_function_json_str,
            population_strategy=population_strategy_json_str)
        population = Population(t=-1, nr_samples=0, epsilon=0)
        abcsmc.populations.append(population)

        if ground_truth_model is not None:  # GT model given
            gt_model = Model(m=ground_truth_model,
                             p_model=1,
                             name=model_names[ground_truth_model])
        else:
            gt_model = Model(m=None,
                             p_model=1,
                             name=None)

        population.models.append(gt_model)
        gt_part = Particle(w=1)
        gt_model.particles.append(gt_part)

        for key, value in ground_truth_parameter.items():
            gt_part.parameters.append(Parameter(name=key, value=value))
        sample = Sample(distance=0)
        gt_part.samples = [sample]
        sample.summary_statistics = [
            SummaryStatistic(name=key, value=value)
            for key, value in observed_summary_statistics.items()
        ]

        for m, name in enumerate(model_names):
            if m != ground_truth_model:
                population.models.append(Model(m=m, name=name, p_model=0))

        self._session.add(abcsmc)
        self._session.commit()
        self.id = abcsmc.id
        history_logger.info("Start {}".format(abcsmc))

    @with_session
    def observed_sum_stat(self):
        sum_stats = (self._session
                     .query(SummaryStatistic)
                     .join(Sample)
                     .join(Particle)
                     .join(Model)
                     .join(Population)
                     .join(ABCSMC)
                     .filter(ABCSMC.id == self.id)
                     .filter(Population.t == -1)
                     .filter(Model.p_model == 1)
                     .all()
                     )
        sum_stats_dct = {ss.name: ss.value for ss in sum_stats}
        return sum_stats_dct

    @property
    @with_session
    def total_nr_simulations(self) -> int:
        """
        Number of sample attempts for the ABC run.

        Returns
        -------

        nr_sim: int
            Total nr of sample attempts for the ABC run.
        """
        nr_sim = (self._session.query(func.sum(Population.nr_samples))
                  .join(ABCSMC).filter(ABCSMC.id == self.id).one()[0])
        return nr_sim

    def _make_session(self):
        # TODO: check if the session creation and closing is still necessary
        # I think I did this funny construction due to some pickling issues
        # but I'm not quite sure anymore
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        engine = create_engine(self.db_identifier,
                               connect_args={'timeout': self.DB_TIMEOUT})
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        session = Session()
        self._session = session
        self._engine = engine
        return session

    def _close_session(self):
        # don't close in memory database
        if self.inmemory:
            return
        # only close connections to permanent databases
        self._session.close()
        self._engine.dispose()
        self._session = None
        self._engine = None

    def __getstate__(self):
        dct = self.__dict__.copy()
        if self.inmemory:
            dct["_engine"] = None
            dct["_session"] = None
        return dct

    @with_session
    @internal_docstring_warning
    def done(self):
        """
        Close database sessions and store end time of population.

        """

        abc_smc_simulation = (self._session.query(ABCSMC)
                              .filter(ABCSMC.id == self.id)
                              .one())
        abc_smc_simulation.end_time = datetime.datetime.now()
        self._session.commit()
        history_logger.info("Done {}".format(abc_smc_simulation))

    @with_session
    def _save_to_population_db(self, t: int, current_epsilon: float,
                               nr_simulations: int,
                               store: dict, model_probabilities: dict,
                               model_names):
        # sqlalchemy experimental stuff and highly inefficient implementation
        # here but that is ok for testing purposes for the moment

        # prepare
        abc_smc_simulation = (self._session.query(ABCSMC)
                              .filter(ABCSMC.id == self.id)
                              .one())

        # store the population
        population = Population(t=t, nr_samples=nr_simulations,
                                epsilon=current_epsilon)
        abc_smc_simulation.populations.append(population)

        for m, model_population in store.items():
            model = Model(m=int(m), p_model=float(model_probabilities[m]),
                          name=str(model_names[m]))
            population.models.append(model)

            for store_item in model_population:
                # a store_item is a Particle
                weight = store_item.weight
                distance_list = store_item.accepted_distances
                parameter = store_item.parameter
                summary_statistics_list = store_item.accepted_sum_stats
                particle = Particle(w=weight)
                model.particles.append(particle)
                for key, value in parameter.items():
                    if isinstance(value, dict):
                        for key_dict, value_dict in value.items():
                            particle.parameters.append(
                                Parameter(name=key + "_" + key_dict,
                                          value=value_dict))
                    else:
                        particle.parameters.append(
                            Parameter(name=key, value=value))
                for distance, summ_stat in zip(distance_list,
                                               summary_statistics_list):
                    sample = Sample(distance=distance)
                    particle.samples.append(sample)
                    for name, value in summ_stat.items():
                        if name is None:
                            raise Exception("Summary statistics need names.")
                        sample.summary_statistics.append(
                            SummaryStatistic(name=name, value=value))

        self._session.commit()
        history_logger.debug("Appended population")

    @internal_docstring_warning
    def append_population(self, t: int,
                          current_epsilon: float,
                          population: Population,
                          nr_simulations: int,
                          model_names):
        """
        Append population to database.

        Parameters
        ----------

        t: int
            Population number.

        current_epsilon: float
            Current epsilon value.

        population: Population
            List of sampled particles.

        nr_simulations: int
            The number of model evaluations for this population.

        model_names: list
            The model names.

        """
        store = population.to_dict()
        model_probabilities = population.get_model_probabilities()

        self._save_to_population_db(t, current_epsilon,
                                    nr_simulations, store, model_probabilities,
                                    model_names)

    @with_session
    def get_model_probabilities(self, t=None) -> pd.DataFrame:
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

        if t is not None:
            t = int(t)

        p_models = (
            self._session
            .query(Model.p_model, Model.m, Population.t)
            .join(Population)
            .join(ABCSMC)
            .filter(ABCSMC.id == self.id)
            .filter(Population.t == t if t is not None else Population.t >= 0)
            .order_by(Model.m)
            .all())
        # TODO this is a mess
        if t is not None:
            p_models_df = pd.DataFrame([p[:2] for p in p_models],
                                       columns=["p", "m"]).set_index("m")
            # TODO the following line is redundant
            # only models with no-zero weight are stored for each population
            p_models_df = p_models_df[p_models_df.p >= 0]
            return p_models_df
        else:
            p_models_df = (pd.DataFrame(p_models, columns=["p", "m", "t"])
                           .pivot("t", "m", "p")
                           .fillna(0))
            return p_models_df

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
        if t is None:
            t = self.max_t
        else:
            t = int(t)
        model_probs = self.get_model_probabilities(t)
        return int((model_probs.p > 0).sum())

    @with_session
    def get_weighted_distances(self, t: Union[int, None]) -> pd.DataFrame:
        """
        Population's weighted distances to the measured sample.
        These weights do not necessarily sum up to 1.
        In case more than one simulation per parameter is performed and
        accepted the sum might be larger.

        Parameters
        ----------

        t: int, None
            Population number.
            If t is None the last population is selected.

        Returns
        -------

        df_weighted: pd.DataFrame
            Weighted distances.
            The dataframe has column "w" for the weights
            and column "distance" for the distances.
        """
        if t is None:
            t = self.max_t
        else:
            t = int(t)

        query = (self._session.query(Sample.distance, Particle.w, Model.m)
                 .join(Particle)
                 .join(Model).join(Population).join(ABCSMC)
                 .filter(ABCSMC.id == self.id)
                 .filter(Population.t == t))
        df = pd.read_sql_query(query.statement, self._engine)

        model_probabilities = self.get_model_probabilities(t).reset_index()
        df_weighted = df.merge(model_probabilities)
        df_weighted["w"] *= df_weighted["p"]
        return df_weighted

    @with_session
    def get_nr_particles_per_population(self) -> pd.Series:
        """

        Returns
        -------

        nr_particles_per_population: pd.DataFrame
            A pandas DataFrame containing the number
            of particles for each population

        """
        query = (self._session.query(Population.t)
                 .join(ABCSMC)
                 .join(Model)
                 .join(Particle)
                 .filter(ABCSMC.id == self.id))
        df = pd.read_sql_query(query.statement, self._engine)
        nr_particles_per_population = df.t.value_counts().sort_index()
        return nr_particles_per_population

    @property
    @with_session
    def max_t(self):
        """
        The population number of the last populations.
        This is equivalent to ``n_populations - 1``.
        """
        max_t = (self._session.query(func.max(Population.t))
                 .join(ABCSMC).filter(ABCSMC.id == self.id).one()[0])
        return max_t

    @property
    def n_populations(self):
        """
        Number of populations stored in the database.
        This is equivalent to ``max_t + 1``.
        """
        return self.max_t + 1

    @with_session
    def get_sum_stats(self, t: int, m: int) -> (np.ndarray, List):
        """
        Summary statistics.

        Parameters
        ----------

        t: int
            Population number

        m: int
            Model index

        Returns
        -------

        w, sum_stats: np.ndarray, list
            * w: the weights associated with the summary statistics
            * sum_stats: list of summary statistics
        """

        # TODO: Is the first output, "weights", needed for anything?

        m = int(m)
        if t is None:
            t = self.max_t
        else:
            t = int(t)

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
                for ss in sample.summary_statistics:
                    sum_stats[ss.name] = ss.value
                results.append(sum_stats)
        return sp.array(weights), results

    @with_session
    def get_weighted_sum_stats(self, t: int=None) -> (List[float], List[dict]):
        """
        Population's weighted summary statistics.
        These weights do not necessarily sum up to 1.
        In case more than one simulation per parameter is performed and
        accepted, the sum might be larger.

        Parameters
        ----------

        t: int, None
            Population number.
            If t is None, the latest population is selected.


        Returns
        -------

        (weights, sum_stats): (List[float], List[dict])
            In the same order in the first array the weights (multiplied by
            the model probabilities), and tin the second array the summary
            statistics.
        """

        if t is None:
            t = self.max_t
        else:
            t = int(t)

        models = (self._session.query(Model)
                  .join(Population).join(ABCSMC)
                  .filter(ABCSMC.id == self.id)
                  .filter(Population.t == t)
                  .all())

        all_weights = []
        all_sum_stats = []

        for model in models:
            for particle in model.particles:
                weight = particle.w * model.p_model
                for sample in particle.samples:
                    # extract sum stats
                    sum_stats = {}
                    for ss in sample.summary_statistics:
                        sum_stats[ss.name] = ss.value
                    all_weights.append(weight)
                    all_sum_stats.append(sum_stats)

        return all_weights, all_sum_stats

    @with_session
    def get_population_strategy(self):
        """

        Returns
        -------
        population_strategy:
            The population strategy.
        """
        abc = self._session.query(ABCSMC).filter(ABCSMC.id == self.id).one()
        return json.loads(abc.population_strategy)

    @with_session
    def get_population_extended(self, *, m=None, t="last", tidy=True) \
            -> pd.DataFrame:
        """
        Get extended population information, including parameters, distances,
        summary statistics, weights and more.

        Parameters
        ----------
        m: int, optional
            The model to query.
            If omitted, all models are returned
        t: str, optional
            Can be "last" or "all"
            In case of "all", all populations are returned.
            If "last", only the last population is returned.
        tidy: bool, optional
            If True, try to return a tidy DataFrame, where the individual
            parameters and summary statistics are pivoted.
            Setting tidy to true will only work for a single model and
            a single population.

        Returns
        -------

        full_population: DataFrame
        """
        query = (self._session.query(Population.t,
                                     Population.epsilon,
                                     Population.nr_samples.label("samples"),
                                     Model.m,
                                     Model.name.label("model_name"),
                                     Model.p_model,
                                     Particle.w,
                                     Particle.id.label("particle_id"),
                                     Sample.distance,
                                     Parameter.name.label("par_name"),
                                     Parameter.value.label("par_val"),
                                     SummaryStatistic.name
                                     .label("sumstat_name"),
                                     SummaryStatistic.value
                                     .label("sumstat_val"),
                                     )
                 .join(ABCSMC)
                 .join(Model)
                 .join(Particle)
                 .join(Sample)
                 .join(SummaryStatistic)
                 .join(Parameter)
                 .filter(ABCSMC.id == self.id)
                 )
        if m is not None:
            query = query.filter(Model.m == m)

        if t == "last":
            t = self.max_t
        if t != "all":
            query = query.filter(Population.t == t)

        df = pd.read_sql_query(query.statement, self._engine)

        if len(df.m.unique()) == 1:
            del df["m"]
            del df["model_name"]
            del df["p_model"]

        if isinstance(t, int):
            del df["t"]

        if tidy:
            if isinstance(t, int) and "m" not in df:
                df = df.set_index("particle_id")
                df_unique = (df[["distance", "w"]]
                             .drop_duplicates())

                df_par = (df[["par_name", "par_val"]]
                          .reset_index()
                          .drop_duplicates(subset=["particle_id",
                                                   "par_name"])
                          .pivot(index="particle_id",
                                 columns="par_name",
                                 values="par_val"))
                df_par.columns = ["par_" + c
                                  for c in df_par.columns]

                df_sumstat = (df[["sumstat_name", "sumstat_val"]]
                              .reset_index()
                              .drop_duplicates(subset=["particle_id",
                                                       "sumstat_name"])
                              .pivot(index="particle_id",
                                     columns="sumstat_name",
                                     values="sumstat_val"))
                df_sumstat.columns = ["sumstat_" + c
                                      for c in df_sumstat.columns]

                df_tidy = (df_unique
                           .merge(df_par,
                                  left_index=True,
                                  right_index=True)
                           .merge(df_sumstat,
                                  left_index=True,
                                  right_index=True))
                df = df_tidy

        return df
