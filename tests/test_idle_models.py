import threading
import unittest
from unittest.mock import Mock, patch

from backend.idle_models import IdleModels


class IdleModelsTests(unittest.TestCase):
    def test_releases_only_after_last_task_and_timeout(self):
        release = Mock(return_value=1)
        cache = IdleModels(release, seconds=60)
        with patch('backend.idle_models.threading.Timer') as timer:
            with cache.activity():
                with cache.activity():
                    cache.release_if_idle(cache._generation)
                    release.assert_not_called()
                timer.assert_not_called()
            callback = timer.call_args.args[1]
            args = timer.call_args.args[2]
            release.assert_not_called()
            callback(*args)
        release.assert_called_once()

    def test_stale_timer_cannot_release_new_or_active_task(self):
        release = Mock()
        cache = IdleModels(release)
        with patch('backend.idle_models.threading.Timer'):
            with cache.activity():
                old_generation = cache._generation
            with cache.activity():
                cache.release_if_idle(old_generation)
                cache.release_if_idle(cache._generation)
            cache.release_if_idle(old_generation)
        release.assert_not_called()

    def test_failure_still_schedules_cleanup(self):
        cache = IdleModels(Mock())
        with patch('backend.idle_models.threading.Timer') as timer:
            with self.assertRaises(ValueError):
                with cache.activity():
                    raise ValueError('task failed')
            timer.return_value.start.assert_called_once()

    def test_release_error_does_not_break_subsequent_activity(self):
        cache = IdleModels(Mock(side_effect=RuntimeError('driver error')))
        cache.release_if_idle(0)
        with patch('backend.idle_models.threading.Timer'):
            with cache.activity():
                pass
        self.assertEqual(cache._active, 0)
