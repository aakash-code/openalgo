"""Taking the index's own move out of the writers' lean, without look-ahead."""

from services.oi_flow_analysis import INDEX_OUT_MIN_STEPS, remove_index_move


def test_pressure_that_only_echoes_the_move_is_taken_out():
    moves = [1.0, -2.0, 3.0, -1.0, 2.0] * 6
    pressure = [10.0 * m for m in moves]
    out = remove_index_move(pressure, moves)
    # Before enough steps, nothing is taken out; after, the echo is gone.
    assert out[:INDEX_OUT_MIN_STEPS] == pressure[:INDEX_OUT_MIN_STEPS]
    assert all(abs(v) < 1e-9 for v in out[INDEX_OUT_MIN_STEPS:])


def test_pressure_the_move_does_not_explain_stays():
    moves = [1.0, -1.0] * 10 + [0.0]
    pressure = [10.0 * m for m in moves[:-1]] + [500.0]
    assert remove_index_move(pressure, moves)[-1] == 500.0


def test_a_step_uses_only_the_steps_before_it():
    moves = [1.0, -1.0] * 10
    pressure = [10.0 * m for m in moves]
    early = remove_index_move(pressure, moves)
    later = remove_index_move(pressure + [99.0], moves + [5.0])
    assert later[: len(early)] == early


def test_an_unknown_move_leaves_the_step_alone():
    moves = [1.0, -1.0] * 10 + [None]
    pressure = [10.0 * m for m in moves[:-1]] + [7.0]
    assert remove_index_move(pressure, moves)[-1] == 7.0
