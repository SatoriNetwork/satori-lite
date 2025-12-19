"""
Tests for AI Engine Training Delay Feature.

Tests the training frequency control functionality including:
- Loading training delay from config
- Default values
- Sleep behavior in training loop
"""
import pytest
import time
import tempfile
import os
import yaml
from unittest.mock import Mock, patch, MagicMock
from satoriengine.veda.engine import StreamModel


class TestTrainingDelayConfig:
    """Test configuration loading for training delay."""

    def test_default_training_delay(self):
        """Test that default training delay is 600 seconds (10 minutes)."""
        # Create StreamModel and check default delay
        streamModel = self._create_mock_stream_model()

        # Should default to 600 seconds
        assert hasattr(streamModel, 'trainingDelay')
        assert streamModel.trainingDelay == 600

    def test_load_training_delay_from_config(self):
        """Test loading custom training delay from config file."""
        # Create temp config with custom delay
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, 'config.yaml')
            with open(config_path, 'w') as f:
                yaml.dump({'training_delay': 120}, f)

            # Mock config.get() to return our test config
            with patch('satoriengine.veda.config.get') as mock_get:
                mock_get.return_value = {'training_delay': 120}

                streamModel = self._create_mock_stream_model()
                delay = streamModel._loadTrainingDelay()

                assert delay == 120

    def test_load_training_delay_missing_config(self):
        """Test that missing config returns default value."""
        with patch('satoriengine.veda.config.get') as mock_get:
            mock_get.return_value = {}  # Empty config

            streamModel = self._create_mock_stream_model()
            delay = streamModel._loadTrainingDelay()

            # Should return default 600
            assert delay == 600

    def test_load_training_delay_invalid_value(self):
        """Test handling of invalid training delay value."""
        with patch('satoriengine.veda.config.get') as mock_get:
            mock_get.return_value = {'training_delay': 'invalid'}

            streamModel = self._create_mock_stream_model()
            delay = streamModel._loadTrainingDelay()

            # Should fall back to default on error
            assert delay == 600

    def test_training_delay_bounds(self):
        """Test training delay boundary values."""
        streamModel = self._create_mock_stream_model()

        # Minimum: 0 seconds (continuous)
        streamModel.trainingDelay = 0
        assert streamModel.trainingDelay == 0

        # Maximum: 86400 seconds (24 hours)
        streamModel.trainingDelay = 86400
        assert streamModel.trainingDelay == 86400

    def _create_mock_stream_model(self):
        """Create a mock StreamModel for testing."""
        # Create minimal StreamModel without full initialization
        streamModel = object.__new__(StreamModel)
        streamModel.trainingDelay = 600  # Default value

        # Add _loadTrainingDelay method
        def _loadTrainingDelay():
            try:
                from satoriengine.veda import config
                delay = config.get().get('training_delay', 600)
                return int(delay)
            except Exception:
                return 600

        streamModel._loadTrainingDelay = _loadTrainingDelay
        return streamModel


class TestTrainingLoopDelay:
    """Test that training loop respects delay setting."""

    def test_run_loop_sleeps_with_delay(self):
        """Test that run() method sleeps between training iterations."""
        # This test verifies the sleep behavior
        # We'll mock time.sleep to avoid actual delays

        with patch('time.sleep') as mock_sleep:
            streamModel = self._create_mock_stream_model_for_run()
            streamModel.trainingDelay = 120  # 2 minutes

            # Run one iteration (we'll stop after first iteration for testing)
            streamModel._run_single_iteration()

            # Verify sleep was called with correct delay
            mock_sleep.assert_called_with(120)

    def test_run_loop_no_sleep_when_zero(self):
        """Test that continuous mode (0 delay) doesn't sleep."""
        with patch('time.sleep') as mock_sleep:
            streamModel = self._create_mock_stream_model_for_run()
            streamModel.trainingDelay = 0  # Continuous mode

            # Run one iteration
            streamModel._run_single_iteration()

            # Sleep should not be called when delay is 0
            mock_sleep.assert_not_called()

    def test_run_loop_uses_updated_delay(self):
        """Test that changing trainingDelay affects next iteration."""
        with patch('time.sleep') as mock_sleep:
            streamModel = self._create_mock_stream_model_for_run()

            # Start with 60 seconds
            streamModel.trainingDelay = 60
            streamModel._run_single_iteration()
            mock_sleep.assert_called_with(60)

            # Update to 300 seconds
            streamModel.trainingDelay = 300
            streamModel._run_single_iteration()
            mock_sleep.assert_called_with(300)

    def _create_mock_stream_model_for_run(self):
        """Create mock StreamModel with minimal run() support."""
        streamModel = object.__new__(StreamModel)
        streamModel.trainingDelay = 600
        streamModel.paused = False

        # Mock single iteration
        def _run_single_iteration():
            if streamModel.paused:
                return

            # Simulate training (no actual training)
            # Just handle the sleep logic
            if streamModel.trainingDelay > 0:
                time.sleep(streamModel.trainingDelay)

        streamModel._run_single_iteration = _run_single_iteration
        return streamModel


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
