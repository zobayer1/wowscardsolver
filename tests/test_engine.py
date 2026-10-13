"""Battle model, roster parsing and search of the engine."""
# pylint: disable=missing-function-docstring

import csv
import io

import pytest

from webapp.engine import solver
from webapp.engine.solver import Card, Enemy, buff, debuff, evaluate


def test_validation_reproduces_in_game_numbers():
    solver.validate()


def test_hp_loss_is_an_additive_debuff():
    # NM at 2 HP with +30% survivability from Kiddo: 50 * (100 + 30 - 10)% = 60
    kiddo = Card("Kiddo", 45, 45, 60, (buff("S", "right"),))
    nm = Card("NM", 50, 50, 15, (), hp=2)
    res = evaluate([kiddo, nm, None, None], [None] * 4)
    assert res[1]["params"] == {"F": 45, "S": 60, "M": 14}


def test_unstoppable_ignores_conditions_but_not_hp_loss():
    fjord = Card("Fjord", 30, 20, 50, (), hp=2)
    res = evaluate([fjord, None, None, None], [Enemy(100, ())] + [None] * 3, {"F": -24},
                   [("Unstoppable",), (), (), ()])
    assert res[0]["params"] == {"F": 27, "S": 18, "M": 45}


def test_debuff_needs_matching_vulnerability_unless_rabid():
    marble = Card("Marble", 30, 30, 40, (debuff(solver.SMOKE, "up"),))
    enemies = [Enemy(100, (solver.AA,))] + [None] * 3
    assert evaluate([marble, None, None, None], enemies)[0]["E"] == 100
    rabid = evaluate([marble, None, None, None], enemies, mods=[("Rabid",), (), (), ()])
    assert rabid[0]["E"] == 80


def parse(text):
    return solver.parse_roster(solver.clean_rows(csv.DictReader(io.StringIO(text))))


HEADER = ("name,firepower,survivability,maneuverability,"
          "ability1_type,ability1_target,ability1_direction\n")


def test_parse_roster_reads_abilities_and_defaults():
    cards = parse(HEADER + "A,1,2,3,buff,Firepower,right\n"
                           "B,4,5,6,debuff,Air Strike,up-left\n"
                           "C,7,8,9,,,\n")
    assert [c.name for c in cards] == ["A", "B", "C"]
    assert cards[0].abilities == (solver.Ability("buff", "F", "right", 30),)
    assert cards[1].abilities == (solver.Ability("debuff", solver.AIR, "up-left", 20),)
    assert cards[2].abilities == () and cards[2].hp == 3 and not cards[2].used


@pytest.mark.parametrize("row, message", [
    ("X,1,2,3,buf,Firepower,right", "line 2: X ability 1: type must be buff or debuff"),
    ("X,1,2,3,buff,Speed,right", "unknown buff target 'Speed'"),
    ("X,1,2,3,debuff,Fire,up", "unknown vulnerability 'Fire'"),
    ("X,1,2,3,debuff,AA Fire,down", "direction must be one of up-left, up, up-right"),
    ("X,one,2,3,,,", "line 2: invalid literal"),
    (",1,2,3,,,", "name is missing"),
])
def test_parse_roster_errors_name_the_line(row, message):
    with pytest.raises(ValueError, match=message):
        parse(HEADER + row + "\n")


def test_parse_roster_missing_column():
    with pytest.raises(ValueError, match="missing column 'maneuverability'"):
        parse("name,firepower,survivability\nX,1,2\n")


def test_solve_stage_respects_used_barred_and_top():
    cards = [Card(f"C{i}", 40, 40, 40, ()) for i in range(6)]
    cards[0] = solver.replace(cards[0], used=True)
    enemies = [Enemy(110, (), 5), Enemy(110, (), 5), None, None]
    kp = solver.KeyPoint("1", enemies, {}, barred=frozenset({"C1"}))
    best, _, _ = solver.solve_stage(cards, [kp], min_wins=2,
                                    limits=solver.SearchLimits(top=2))
    assert len(best) == 2
    for combo, st in best:
        names = {c.name for c in combo[0][1] if c}
        assert names.isdisjoint({"C0", "C1"}) and len(names) == 2
        assert st["p_goal"] == 1.0


# ---------------------------------------------------------------- buff / debuff power conditions
SMOKE_ENEMY = [Enemy(100, (solver.SMOKE,))] + [None] * 3


def enemy_power(cond=None, mods=None, pct=20):
    marble = Card("Marble", 30, 30, 40, (debuff(solver.SMOKE, "up", pct),))
    return evaluate([marble, None, None, None], SMOKE_ENEMY, cond,
                    [mods or (), (), (), ()])[0]["E"]


def test_debuff_power_condition_scales_debuffs():
    assert enemy_power() == 80
    assert enemy_power({"D": -50}) == 90                     # 20% -> 10%
    assert enemy_power({"D": -30}) == 86                     # 20% -> 14%
    assert enemy_power({"D": -50}, ("Specialization",)) == 70  # 20% x (1 + 1 - 0.5) = 30%
    assert enemy_power({"D": -200}) == 100                   # never turns into a buff
    assert enemy_power({"D": -50}, pct=25) == 88             # 12.5% -> 87.5 rounds to 88


def test_unstoppable_ignores_debuff_power_condition():
    assert enemy_power({"D": -50}, ("Unstoppable",)) == 80


def test_buff_power_condition_scales_buffs():
    kiddo = Card("Kiddo", 45, 45, 60, (buff("S", "right"),))
    nm = Card("NM", 50, 50, 15, ())
    def nm_s(cond=None, mods=None):
        res = evaluate([kiddo, nm, None, None], [None] * 4, cond, [mods or (), (), (), ()])
        return res[1]["params"]["S"]
    assert nm_s() == 65                                      # +30%
    assert nm_s({"B": -50}) == 58                            # +15%: 57.5 rounds up
    assert nm_s({"B": -50}, ("Teamwork",)) == 73             # 30% x 1.5 = +45%
    assert nm_s({"B": -50}, ("Unstoppable",)) == 65


def test_environment_csv_reads_buff_and_debuff_power(tmp_path):
    env = tmp_path / "environment.csv"
    env.write_text("setting,keypoint,value\nDebuff power,2,-50\nBuff power,,-20\n")
    _, conds, _, _, _, _ = solver.load_environment(env)
    assert conds == {"2": {"D": -50}, "*": {"B": -20}}
    assert solver.cond_for(conds, "2") == {"B": -20, "D": -50}


def test_barred_type_in_environment_csv(tmp_path):
    board = tmp_path / "board.csv"
    board.write_text("keypoint,slot,power\n1,1,100\n2,1,100\n")
    env = tmp_path / "environment.csv"
    env.write_text("setting,keypoint,value\nBarred type,2,flan\nBarred,,C\n")
    roster = [Card("A", 1, 1, 1, (), type="flan"), Card("B", 1, 1, 1, (), type="flan"),
              Card("C", 1, 1, 1, (), type="cuzr"), Card("D", 1, 1, 1, ())]
    keypoints, _ = solver.load_stage(board, env, roster)
    assert keypoints[0].barred == {"C"}
    assert keypoints[1].barred == {"A", "B", "C"}
    env.write_text("setting,keypoint,value\nBarred type,,nope\n")
    with pytest.raises(AssertionError, match="barred type 'nope'"):
        solver.load_stage(board, env, roster)


def test_parse_roster_reads_type():
    cards = parse(HEADER.replace("\n", ",type\n") + "A,1,2,3,,,,doddy\nB,1,2,3,,,,\n")
    assert [c.type for c in cards] == ["doddy", ""]
    assert parse(HEADER + "A,1,2,3,,,\n")[0].type is None
