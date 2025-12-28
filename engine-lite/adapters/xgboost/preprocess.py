import pandas as pd
from datetime import timedelta, datetime
import numpy as np

class XgbProcessedData:
    def __init__(self, dataset: pd.DataFrame, sampling_frequency: str):
        self.dataset = dataset
        self.sampling_frequency = sampling_frequency

def xgbDataPreprocess(data: pd.DataFrame) -> XgbProcessedData:

    def _roundToMinute(roundingdatetime: timedelta) -> int:
        rounded = roundingdatetime + timedelta(seconds=30)
        total_minutes = rounded.total_seconds() / 60
        minutes = int(total_minutes % 60)
        return minutes

    def _roundToNearestMinute(dt: datetime) -> datetime:
        return dt.replace(second=0, microsecond=0) + timedelta(
            minutes=1 if dt.second >= 30 else 0)

    def _processNoisyDataset(
        df: pd.DataFrame,
        round_to_hours: int,
        round_to_minutes: int,
        round_to_seconds: int = 0,
        offset_hours: int = 0,
        offset_minutes: int = 0,
        offset_seconds: int = 0,
        datetime_column: str = "date_time"
    ) -> pd.DataFrame:
        df_copy = df.copy()
        if isinstance(df_copy.index, pd.DatetimeIndex):
            datetime_series = df_copy.index
            is_index = True
        elif datetime_column in df_copy.columns:
            datetime_series = pd.to_datetime(df_copy[datetime_column].str.strip(), errors='coerce')
            is_index = False
        else:
            raise ValueError(f"Datetime column '{datetime_column}' not found in DataFrame")
        first_day = _roundToNearestMinute(datetime_series.min())
        rounded_datetimes = datetime_series.map(
            lambda x: _roundTime(
                first_day,
                x,
                round_to_hours,
                round_to_minutes,
                round_to_seconds,
                offset_hours,
                offset_minutes,
                offset_seconds))
        if is_index:
            df_copy.index = rounded_datetimes
        else:
            df_copy[datetime_column] = rounded_datetimes
        return df_copy

    def _roundTime(
        first_day: datetime,
        dt: datetime,
        round_to_hours: int,
        round_to_minutes: int,
        round_to_seconds: int,
        offset_hours: int = 0,
        offset_minutes: int = 0,
        offset_seconds: int = 0
    ) -> datetime:
        dt = dt + timedelta(
            hours=offset_hours,
            minutes=offset_minutes,
            seconds=offset_seconds)
        total_seconds = (round_to_hours * 3600) + (round_to_minutes * 60) + round_to_seconds

        # Prevent division by zero - if no rounding interval specified, return as-is
        if total_seconds == 0:
            return dt - timedelta(hours=offset_hours, minutes=offset_minutes, seconds=offset_seconds)

        seconds_since_first = round((dt - first_day).total_seconds())
        rounded_seconds = round(seconds_since_first / total_seconds) * total_seconds
        rounded_dt = first_day + timedelta(seconds=rounded_seconds)
        rounded_dt -= timedelta(
            hours=offset_hours,
            minutes=offset_minutes,
            seconds=offset_seconds)
        return rounded_dt

    def fmt(sf):
        return "".join(
            f"{v}{abbr[k]}"
            for k, v in sf.components._asdict().items()
            if v != 0)

    raw_dataset = data
    # Handle both Unix timestamps (as strings or numbers) and date strings
    try:
        # Try to convert to numeric - if successful, these might be Unix timestamps
        numeric_times = pd.to_numeric(raw_dataset["date_time"], errors='coerce')
        # Valid Unix timestamps should be > 946684800 (year 2000) and < 4102444800 (year 2100)
        # This prevents misinterpreting small numbers (like year "2025") as timestamps
        if (numeric_times.notna().all() and
            numeric_times.min() > 946684800 and
            numeric_times.max() < 4102444800):
            # All values are numeric and in valid Unix timestamp range
            raw_dataset["date_time"] = pd.to_datetime(numeric_times, unit='s', utc=True)
        else:
            # Contains non-numeric values or out of range - treat as date strings
            raw_dataset["date_time"] = pd.to_datetime(raw_dataset["date_time"], utc=True)
    except Exception:
        # Fallback to default parsing if numeric conversion fails
        raw_dataset["date_time"] = pd.to_datetime(raw_dataset["date_time"], utc=True)
    raw_dataset = raw_dataset.set_index("date_time")
    raw_diff_dat = raw_dataset.index.to_series().diff()
    value_counts = raw_diff_dat.value_counts().sort_index()
    num_distinct_values = len(value_counts)
    if num_distinct_values > (len(raw_dataset) * 0.05):
        median = raw_dataset.index.to_series().diff().median()
        if median < timedelta(hours=1, minutes=0, seconds=29):
            if median >= timedelta(minutes=59, seconds=29):
                round_to_hour = 1
                round_to_minute = 0
            else:
                round_to_hour = 0
                round_to_minute = _roundToMinute(median)
        else:
            round_to_hour = median.total_seconds() // 3600
            round_to_minute = _roundToMinute(
                median - timedelta(hours=round_to_hour, minutes=0, seconds=0))
        dataset = _processNoisyDataset(
            raw_dataset,
            round_to_hours=round_to_hour,
            round_to_minutes=round_to_minute)
    else:
        dataset = raw_dataset
    sf = dataset.index.to_series().diff().median()
    abbr = {
        "days": "d",
        "hours": "h",
        "minutes": "min",
        "seconds": "s",
        "milliseconds": "ms",
        "microseconds": "us",
        "nanoseconds": "ns"}
    if isinstance(sf, pd.Timedelta):
        sampling_frequency = fmt(sf)
    elif isinstance(sf, pd.TimedeltaIndex):
        sampling_frequency = sf.map(fmt)
    else:
        raise ValueError
    dataset_averaged = dataset.groupby(level=0).agg({
        "value": "mean",
        "id": "first"})
    dataset = dataset_averaged
    dataset = dataset.asfreq(sampling_frequency, method="nearest", fill_value=np.nan)
    return XgbProcessedData(dataset, sampling_frequency)

def _prepareTimeFeatures(df: pd.DataFrame) -> pd.DataFrame:
        """Convert datetime series into numeric features for XGBoost"""
        df['hour'] = df.index.hour
        df['day'] = df.index.day
        df['month'] = df.index.month
        df['year'] = df.index.year
        df['day_of_week'] = df.index.dayofweek
        return df
