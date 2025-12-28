'''
run chronos on the data
produce a feature of predictions
feed data and chronos predictions into xgboost
'''
from typing import Union
import os
import joblib
import numpy as np
import pandas as pd
import datetime
from xgboost import XGBRegressor
from sklearn.metrics import mean_absolute_error
from sklearn.model_selection import train_test_split
from satorilib.logging import info, debug, warning
from satoriengine.veda.adapters.interface import ModelAdapter, TrainingResult
from satoriengine.veda.adapters.xgbchronos.chronos_adapter import PretrainedChronosAdapter


class XgbChronosAdapter(ModelAdapter):

    @staticmethod
    def condition(*args, **kwargs) -> float:
        if (
            isinstance(kwargs.get('availableRamGigs'), float)
            and kwargs.get('availableRamGigs') < .1
        ):
            return 0
        if 20 <= len(kwargs.get('data', [])) < 1_000:
            return 1.0
        return 0.0

    def __init__(self, uid: str = None, modelPath: str = None, **kwargs):
        super().__init__()
        self.uid = uid
        self.model: XGBRegressor = None
        self.modelError: float = None
        self.modelPath = modelPath
        self.chronos: Union[PretrainedChronosAdapter, None] = PretrainedChronosAdapter()
        self.dataset: pd.DataFrame = None
        self.hyperparameters: Union[dict, None] = None
        self.trainX: pd.DataFrame = None
        self.testX: pd.DataFrame = None
        self.trainY: np.ndarray = None
        self.testY: np.ndarray = None
        self.split: float = None
        self.rng = np.random.default_rng(datetime.datetime.now().microsecond // 100)

    @staticmethod
    def _load(modelPath: str, **kwargs) -> Union[None, XGBRegressor]:
        """loads and returns the model model from disk if present"""
        try:
            return joblib.load(modelPath)
        except Exception as e:
            debug(f"unable to load model file, creating a new one: {e}", print=True)
            if os.path.isfile(modelPath):
                os.remove(modelPath)
            return None

    def load(self, modelPath: str, **kwargs) -> Union[None, XGBRegressor]:
        """loads the model model from disk if present"""
        modelPath = self._setModelPath(modelPath)
        saved = XgbChronosAdapter._load(modelPath, **kwargs)
        if saved is None:
            try:
                if 'XgbChronosAdapter' not in modelPath:
                    modelPath = '/'.join(modelPath.split('/')[:-1]) + '/' + 'XgbChronosAdapter.joblib'
                    return self.load(modelPath)
            except Exception as _:
                pass
            return None
        self.model = saved['stableModel']
        self.modelError = saved['modelError']
        self.dataset = saved['dataset']
        return self.model

    @staticmethod
    def _save(
        model: XGBRegressor,
        modelError: float,
        dataset: pd.DataFrame,
        modelPath: str,
        **kwargs,
    ) -> bool:
        """saves the stable model to disk"""
        try:
            os.makedirs(os.path.dirname(modelPath), exist_ok=True)
            joblib.dump({
                'stableModel': model,
                'modelError': modelError,
                'dataset': dataset}, modelPath)
            return True
        except Exception as e:
            warning(f"Error saving model: {e}")
            return False

    def save(self, modelPath: str = None, **kwargs) -> bool:
        """saves the stable model to disk"""
        modelPath = self._setModelPath(modelPath)
        try:
            os.makedirs(os.path.dirname(modelPath), exist_ok=True)
            self.modelError = self.score()
            joblib.dump({
                'stableModel': self.model,
                'modelError': self.modelError,
                'dataset': self.dataset}, modelPath)
            return True
        except Exception as e:
            warning(f"Error saving model: {e}")
            return False

    def compare(self, other: Union[ModelAdapter, None] = None, **kwargs) -> bool:
        """
        Compare other (model) and this models based on their backtest error.
        Returns True if this model performs better than other model.
        """
        if not isinstance(other, self.__class__):
            return True
        thisScore = self.score()
        try:
            otherScore = other.score(test_x=self.testX, test_y=self.testY)
        except Exception as e:
            warning('unable to score properly:', e)
            otherScore = 0.0
        isImproved = thisScore < otherScore
        if isImproved:
            info(
                'model improved!'
                f'\n  stable score: {otherScore}'
                f'\n  pilot  score: {thisScore}'
                f'\n  Parameters: {self.hyperparameters}',
                color='green')
        else:
            debug(
                f'\nstable score: {otherScore}'
                f'\npilot  score: {thisScore}')
            self._update(other)
        return isImproved

    def score(self, test_x=None, test_y=None, **kwargs) -> float:
        """ Will score the model """
        if self.model is None:
            return np.inf
        self.modelError = mean_absolute_error(
            test_y if test_y is not None else self.testY,
            self.model.predict(test_x if test_x is not None else self.testX))
        return self.modelError

    def fit(self, data: pd.DataFrame, **kwargs) -> TrainingResult:
        """ Train a new model """
        if self.chronos.model is None:
            return TrainingResult(0, self)
        self._manageData(data)
        x = self.dataset.iloc[:-1, :-1]
        y = self.dataset.iloc[:-1, -1]
        pre_trainX, pre_testX, self.trainY, self.testY = train_test_split(
            x,
            y,
            test_size=self.split or 0.2,
            shuffle=False,
            random_state=37)
        self.trainX = pre_trainX.reset_index(drop=True)
        self.testX = pre_testX.reset_index(drop=True)
        self.hyperparameters = self._mutateParams(
            prevParams=self.hyperparameters,
            rng=self.rng)
        if self.model is None:
            self.model = XGBRegressor(**self.hyperparameters)
        else:
            self.model.set_params(**self.hyperparameters)
        self.model.fit(
            self.trainX,
            self.trainY,
            eval_set=[(self.trainX, self.trainY), (self.testX, self.testY)],
            verbose=False)
        return TrainingResult(1, self)

    def predict(self, data: pd.DataFrame, **kwargs) -> Union[pd.DataFrame, None]:
        """Make predictions using the stable model"""
        if self.model is None:
            return None
        if self.dataset is None:
            return None
        self._manageData(data, chronosOnLast=True)
        featureSet = self.dataset.iloc[[-1], :-1]
        prediction = self.model.predict(featureSet)
        frequency = self._getSamplingFrequency(self.dataset)
        futureDates = pd.date_range(
            start=pd.Timestamp(self.dataset.index[-1]) + pd.Timedelta(frequency),
            periods=1,
            freq=frequency)
        result_df = pd.DataFrame({'date_time': futureDates, 'pred': prediction})
        return result_df

    def _setModelPath(self, modelPath: str = None) -> str:
        self.modelPath = self.modelPath or modelPath
        modelPath = modelPath or self.modelPath
        return modelPath

    def _update(self, other: Union['XgbChronosAdapter', None] = None, **kwargs) -> bool:
        """
        we save chronos predictions to the dataset slowly over time, since they
        are technically part of the model we want to save them as we go. So we
        must combine the existing best model with the latest chronos predictions
        """
        if not isinstance(other, self.__class__):
            return True
        if (len(self.dataset[~self.dataset['chronos'].isna()]) > len(
                other.dataset[~other.dataset['chronos'].isna()])
        ):
            saved = XgbChronosAdapter._load(other.modelPath)
            if saved is None:
                try:
                    if 'XgbChronosPipeline' not in other.modelPath:
                        modelPath = '/'.join(other.modelPath.split('/')[:-1]) + '/' + 'XgbChronosPipeline.joblib'
                        saved = XgbChronosAdapter._load(modelPath)
                        XgbChronosAdapter._save(
                            model=saved['stableModel'],
                            modelError=saved['modelError'],
                            dataset=self.dataset,
                            modelPath=other.modelPath)
                except Exception as _:
                    pass
            else:
                XgbChronosAdapter._save(
                    model=saved['stableModel'],
                    modelError=saved['modelError'],
                    dataset=self.dataset,
                    modelPath=other.modelPath)

    def _getSamplingFrequency(self, dataset: pd.DataFrame):

        def fmt(sf):
            return "".join(
                f"{v}{abbr[k]}"
                for k, v in sf.components._asdict().items()
                if v != 0)

        sf = dataset.index.to_series().diff().median()
        # Convert to frequency string
        abbr = {
            "days": "d",
            "hours": "h",
            "minutes": "min",
            "seconds": "s",
            "milliseconds": "ms",
            "microseconds": "us",
            "nanoseconds": "ns"}
        if isinstance(sf, pd.Timedelta):
            return fmt(sf)
        elif isinstance(sf, pd.TimedeltaIndex):
            return sf.map(fmt)
        else:
            raise ValueError

    def _manageData(self, data: pd.DataFrame, chronosOnLast:bool=False) -> tuple[pd.DataFrame, str]:
        '''
        here we need to merge the chronos predictions with the data, but it
        must be done incrementally because it takes too long to do it on the
        whole dataset everytime so we save the processed data and
        incrementally add to it over time.
        '''

        def updateData(data: pd.DataFrame) -> pd.DataFrame:

            def conformData(data: pd.DataFrame) -> pd.DataFrame:
                # Handle both Unix timestamps (as strings or numbers) and date strings
                try:
                    # Try to convert to numeric - if successful, these might be Unix timestamps
                    numeric_times = pd.to_numeric(data['date_time'], errors='coerce')
                    # Valid Unix timestamps should be > 946684800 (year 2000) and < 4102444800 (year 2100)
                    # This prevents misinterpreting small numbers (like year "2025") as timestamps
                    if (numeric_times.notna().all() and
                        numeric_times.min() > 946684800 and
                        numeric_times.max() < 4102444800):
                        # All values are numeric and in valid Unix timestamp range
                        data['date_time'] = pd.to_datetime(numeric_times, unit='s', utc=True)
                    else:
                        # Contains non-numeric values or out of range - treat as date strings
                        data['date_time'] = pd.to_datetime(data['date_time'])
                except Exception:
                    # Fallback to default parsing if numeric conversion fails
                    data['date_time'] = pd.to_datetime(data['date_time'])
                data['date_time'] = data['date_time'].dt.strftime('%Y-%m-%d %H:%M:%S')
                data['date_time'] = pd.to_datetime(
                    data['date_time'],
                    format='%Y-%m-%d %H:%M:%S')
                data = data.set_index('date_time')
                data.drop(['id'], axis=1, inplace=True)
                data['hour'] = data.index.hour  # (0-23)
                data['dayofweek'] = data.index.dayofweek  # (0=Monday, 6=Sunday)
                data['month'] = data.index.month  # (1-12)
                return data

            data = conformData(data)
            # incrementally add missing processed data rows to the self.dataset
            if self.dataset is None:
                self.dataset = data
                self.dataset['chronos'] = np.nan
            else:
                # Identify rows in procData.dataset not present in self.dataset
                missingRows = data[~data.index.isin(self.dataset.index)]
                # Append only the missing rows to self.dataset
                self.dataset = pd.concat([self.dataset, missingRows])
            # potential = self.dataset.drop_duplicates(subset='value', keep='first')
            # if len(potential) >= 20:
            #     return potential
            return self.dataset

        def addPercentageChange(df: pd.DataFrame) -> pd.DataFrame:

            def calculatePercentageChange(df, past):
                return (
                    (df['value'] - df['value'].shift(past)) /
                    df['value'].shift(past)
                ) * 100

            for past in [1, 2, 3, 5, 8, 13, 21, 34, 55]:
                df[f'percent{past}'] = calculatePercentageChange(df, past)
            return df

        def addChronos(df: pd.DataFrame) -> pd.DataFrame:
            # now look at the self.dataset and where the chronos column is empty run the chronos prediction for it, filling the nan column at that row:
            # Ensure the dataset is sorted by timestamp (index)
            df.sort_index(inplace=True)
            # Identify rows where the 'chronos' column is NaN - skip first row
            unpredicted = df.iloc[1:][df['chronos'].isna()]
            # Process rows with missing 'chronos' one at a time
            i = 0
            for idx, row in unpredicted.iterrows():
                if chronosOnLast:
                    historicalData = df.loc[:idx]
                else:
                    # Slice the dataset up to (but not including) the current timestamp
                    historicalData = df.loc[:idx].iloc[:-1]
                # print(historicalData)
                # Ensure historicalData is non-empty before calling predict
                if not historicalData.empty:
                    # Predict and fill the 'chronos' value for the current row
                    df.at[idx, 'chronos'] = self.chronos.predict(data=historicalData[['value']])
                # adding this data can be slow, so we'll just do a few at a time
                i += 1
                if i > 4:
                    break
            return df

        def clearoutInfinities(df: pd.DataFrame) -> pd.DataFrame:
            """
            Replace positive infinity with the largest finite value in the column
            and negative infinity with the smallest finite value in the column.
            """
            for col in df.columns:
                if df[col].dtype.kind in 'bifc':  # Check if the column is numeric
                    max_val = df[col][~df[col].isin([np.inf, -np.inf])].max()  # Largest finite value
                    min_val = df[col][~df[col].isin([np.inf, -np.inf])].min()  # Smallest finite value
                    df[col] = df[col].replace(np.inf, max_val)
                    df[col] = df[col].replace(-np.inf, min_val)
            #self.dataset = self.dataset.select_dtypes(include=[np.number])  # Ensure only numeric data
            return df

        self.dataset = updateData(data)
        self.dataset = addPercentageChange(self.dataset)
        self.dataset = addChronos(self.dataset)
        self.dataset = clearoutInfinities(self.dataset)
        self.dataset['tomorrow'] = self.dataset['value'].shift(-1)
        return self.dataset


    @staticmethod
    def paramBounds() -> dict:
        return {
            'n_estimators': (100, 2000),
            'max_depth': (3, 10),
            'learning_rate': (0.005, 0.3),
            'subsample': (0.6, 1.0),
            'colsample_bytree': (0.6, 1.0),
            'min_child_weight': (1, 10),
            'gamma': (0, 1),
            'scale_pos_weight': (0.5, 10)}

    @staticmethod
    def _prepParams(rng: Union[np.random.Generator, None] = None) -> dict:
        """
        Generates randomized hyperparameters for XGBoost within reasonable ranges.
        Returns a dictionary of hyperparameters.
        """
        paramBounds: dict = XgbChronosAdapter.paramBounds()
        rng = rng or np.random.default_rng(37)
        params = {
            'random_state': rng.integers(0, 10000),
            'eval_metric': 'mae',
            'learning_rate': rng.uniform(
                paramBounds['learning_rate'][0],
                paramBounds['learning_rate'][1]),
            'subsample': rng.uniform(
                paramBounds['subsample'][0],
                paramBounds['subsample'][1]),
            'colsample_bytree': rng.uniform(
                paramBounds['colsample_bytree'][0],
                paramBounds['colsample_bytree'][1]),
            'gamma': rng.uniform(
                paramBounds['gamma'][0],
                paramBounds['gamma'][1]),
            'n_estimators': rng.integers(
                paramBounds['n_estimators'][0],
                paramBounds['n_estimators'][1]),
            'max_depth': rng.integers(
                paramBounds['max_depth'][0],
                paramBounds['max_depth'][1]),
            'min_child_weight': rng.integers(
                paramBounds['min_child_weight'][0],
                paramBounds['min_child_weight'][1]),
            'scale_pos_weight': rng.uniform(
                paramBounds['scale_pos_weight'][0],
                paramBounds['scale_pos_weight'][1])}
        return params

    @staticmethod
    def _mutateParams(
        prevParams: Union[dict, None] = None,
        rng: Union[np.random.Generator, None] = None,
    ) -> dict:
        """
        Tweaks the previous hyperparameters for XGBoost by making random adjustments
        based on a squished normal distribution that respects both boundaries and the
        relative position of the current value within the range.
        Args:
            prevParams (dict): A dictionary of previous hyperparameters.
        Returns:
            dict: A dictionary of tweaked hyperparameters.
        """
        rng = rng or np.random.default_rng(37)
        prevParams = prevParams or XgbChronosAdapter._prepParams(rng)
        paramBounds: dict = XgbChronosAdapter.paramBounds()
        mutatedParams = {}
        for param, (minBound, maxBound) in paramBounds.items():
            currentValue = prevParams[param]
            rangeSpan = maxBound - minBound
            # Generate a symmetric tweak centered on the current value
            stdDev = rangeSpan * 0.1  # 10% of the range as standard deviation
            tweak = rng.normal(0, stdDev)
            # Adjust the parameter and ensure it stays within bounds
            newValue = currentValue + tweak
            newValue = max(minBound, min(maxBound, newValue))
            # Ensure integers for appropriate parameters
            if param in ['n_estimators', 'max_depth', 'min_child_weight']:
                newValue = int(round(newValue))
            mutatedParams[param] = newValue
        # to handle static parameters... we should keep random_state static
        # because we're exploring the hyperparameter state space relative to it
        mutatedParams['random_state'] = prevParams['random_state']
        mutatedParams['eval_metric'] = 'mae'
        return mutatedParams


    @staticmethod
    def _straight_line_interpolation(df, value_col, step='10T', scale=0.0, rng: Union[np.random.Generator, None] = None):
        """
        This would probably be better to use than the stepwise pattern as it
        atleast points in the direction of the trend.
        Performs straight line interpolation on missing timestamps.
        Parameters:
        - df: DataFrame with a datetime index and a column to interpolate.
        - value_col: The column name with values to interpolate.
        - step: The frequency to use for resampling (e.g., '10T' for 10 minutes).
        Returns:
        - DataFrame with interpolated values.
        """
        # Ensure the DataFrame has a DatetimeIndex
        if not isinstance(df.index, pd.DatetimeIndex):
            if 'date_time' in df.columns:
                df['date_time'] = pd.to_datetime(df['date_time'])
                df.set_index('date_time', inplace=True)
            else:
                raise ValueError("The DataFrame must have a DatetimeIndex or a 'date_time' column.")
        # Sort the index and resample
        df = df.sort_index()
        df = df.resample(step).mean()  # Resample to fill in missing timestamps with NaN
        # Perform fractal interpolation
        rng = rng or np.random.default_rng(seed=37)
        for _ in range(5):  # Number of fractal iterations
            filled = df[value_col].interpolate(method='linear')  # Linear interpolation
            perturbation = rng.normal(scale=scale, size=len(filled))  # Small random noise
            df[value_col] = filled + perturbation  # Add fractal-like noise
        return df

    @staticmethod
    def merge(dfs: list[pd.DataFrame], targetColumn: Union[str, tuple[str]]):
        ''' Layer 1
        combines multiple mutlicolumned dataframes.
        to support disparate frequencies,
        outter join fills in missing values with previous value.
        filters down to the target column observations.
        '''
        from functools import reduce
        import pandas as pd
        if len(dfs) == 0:
            return None
        if len(dfs) == 1:
            return dfs[0]
        for ix, item in enumerate(dfs):
            if targetColumn in item.columns:
                dfs.insert(0, dfs.pop(ix))
                break
            # if we get through this loop without hitting the if
            # we could possibly use that as a trigger to use the
            # other merge function, also if targetColumn is None
            # why would we make a dataset without target though?
        for df in dfs:
            df.index = pd.to_datetime(df.index)
        return reduce(
            lambda left, right:
                pd.merge_asof(left, right, left_index=True, right_index=True),
            dfs)
