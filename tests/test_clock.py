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


def test_guard_now_defaults_to_walltime():
    import time
    assert abs(clock.guard_now() - time.time()) < 1.0


def test_set_clock_does_not_move_guard_clock():
    """核心不變量：backtester 的 set_clock() 不得動到守衛時鐘。

    兩者若共用，一邊實盤一邊回測會讓 quote_age 變成巨大負數、全面停單。
    """
    import time
    clock.set_clock(lambda: 1_600_000_000.0)
    try:
        assert clock.now() == 1_600_000_000.0
        assert abs(clock.guard_now() - time.time()) < 1.0
    finally:
        clock.reset_clock()


def test_set_guard_clock_does_not_move_now():
    import time
    clock.set_guard_clock(lambda: 42.0)
    try:
        assert clock.guard_now() == 42.0
        assert abs(clock.now() - time.time()) < 1.0
    finally:
        clock.reset_guard_clock()
    assert abs(clock.guard_now() - time.time()) < 1.0
