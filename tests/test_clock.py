from grid_engine import clock


def test_default_now_is_walltime():
    import time
    assert abs(clock.now() - time.time()) < 1.0


def test_set_clock_overrides():
    clock.set_clock(lambda: 123.0)
    try:
        assert clock.now() == 123.0
    finally:
        clock.reset_clock()


def test_reset_restores_walltime():
    clock.set_clock(lambda: 1.0)
    clock.reset_clock()
    import time
    assert abs(clock.now() - time.time()) < 1.0
