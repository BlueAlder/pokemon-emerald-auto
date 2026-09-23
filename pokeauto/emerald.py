"""Pokemon Emerald's critical path, from power-on to Hall of Fame.

Each milestone names what the game records when that beat is finished (a flag
or var set by the story script, a badge, an item) and the actions that get
there. Coordinates and object ids come from the decomp's map data; the
planner does all the walking.

Assumes the default player (Brendan); rival house maps are chosen by gender.
"""
from __future__ import annotations

from .route import (Milestone, all_of, any_of, badges, call, flag, goto, has_item, interact,
                    party_size, prefer, reachable, answer, talk, talk_s, trainer_beaten, unless,
                    var_ge)


def _male(a) -> bool:
    return a.game.avatar()["gender"] == 0


def home(floor: int):
    def act(a):
        who = "BRENDANS" if _male(a) else "MAYS"
        a.goto(f"MAP_LITTLEROOT_TOWN_{who}_HOUSE_{floor}F")
    act.__name__ = f"home {floor}F"
    return act


def rival_house(floor: int):
    def act(a):
        who = "MAYS" if _male(a) else "BRENDANS"
        a.goto(f"MAP_LITTLEROOT_TOWN_{who}_HOUSE_{floor}F")
    act.__name__ = f"rival house {floor}F"
    return act


def set_clock(a):
    who = "BRENDANS" if _male(a) else "MAYS"
    x = 5 if _male(a) else 3
    a.interact(f"MAP_LITTLEROOT_TOWN_{who}_HOUSE_2F", x, 1, "up")


def touch_rival_ball(a):
    if _male(a):
        a.interact("MAP_LITTLEROOT_TOWN_MAYS_HOUSE_2F", 5, 4)
    else:
        a.interact("MAP_LITTLEROOT_TOWN_BRENDANS_HOUSE_2F", 3, 4)


def choose_starter(a):
    """Open Birch's bag and take Mudkip (right-hand ball)."""
    a.ctl.ui_handlers["StarterChoose"] = _starter_ui
    a.talk("MAP_ROUTE101", 3)


def _starter_ui(ctl, m):
    game = ctl.game
    if any("Task_HandleStarterChooseInput" in t for t in m.tasks):
        sel = game.task_data("Task_HandleStarterChooseInput")[0]
        if sel < 2:
            ctl.press("RIGHT", release=8)
        else:
            ctl.press("A", release=20)
    elif any("Task_HandleConfirmStarterInput" in t for t in m.tasks):
        ctl._menu_select(0)
    else:
        ctl.idle(4)


ROUTE: list[Milestone] = [
    Milestone("leave_truck", var_ge("VAR_LITTLEROOT_INTRO_STATE", 3), [
        call("boot"),
        lambda a: a.ctl.goto(lambda s: s.map != "MAP_INSIDE_OF_TRUCK", desc="out of the truck"),
    ], heal_first=False),
    Milestone("set_clock", flag("FLAG_SET_WALL_CLOCK"), [home(2), set_clock], heal_first=False),
    Milestone("options", lambda a: a.game.options()["text_speed"] == 2
              and a.game.options()["battle_scene_off"], [call("set_options")], heal_first=False),
    Milestone("watch_tv", var_ge("VAR_LITTLEROOT_INTRO_STATE", 7), [home(1)], heal_first=False),
    Milestone("meet_rival_mom", flag("FLAG_MET_RIVAL_MOM"), [rival_house(1)], heal_first=False),
    Milestone("meet_rival", var_ge("VAR_LITTLEROOT_RIVAL_STATE", 3),
              [rival_house(2), touch_rival_ball], heal_first=False),
    Milestone("birch_rescue", var_ge("VAR_ROUTE101_STATE", 2),
              [goto("MAP_ROUTE101", 10, 19)], heal_first=False),
    Milestone("starter", flag("FLAG_SYS_POKEMON_GET"), [choose_starter], heal_first=False),
    Milestone("back_to_lab", var_ge("VAR_BIRCH_LAB_STATE", 3),
              [goto("MAP_LITTLEROOT_TOWN_PROFESSOR_BIRCHS_LAB")], heal_first=False),
    Milestone("rival_route103", var_ge("VAR_BIRCH_LAB_STATE", 4),
              [goto("MAP_ROUTE103"), talk("MAP_ROUTE103", 2)], min_level=7, important=True),
    Milestone("pokedex", flag("FLAG_RECEIVED_POKEDEX_FROM_BIRCH"),
              [goto("MAP_LITTLEROOT_TOWN_PROFESSOR_BIRCHS_LAB")]),
    Milestone("running_shoes", flag("FLAG_RECEIVED_RUNNING_SHOES"),
              [goto("MAP_ROUTE101")]),

    # -- Petalburg, the woods, Rustboro -------------------------------------------
    Milestone("meet_dad", var_ge("VAR_PETALBURG_GYM_STATE", 2),
              [goto("MAP_PETALBURG_CITY_GYM"),
               talk_s("MAP_PETALBURG_CITY_GYM", "EventScript_Norman")]),
    Milestone("petalburg_woods", var_ge("VAR_PETALBURG_WOODS_STATE", 1),
              [goto("MAP_PETALBURG_WOODS", 26, 23)], min_level=10),
    Milestone("reach_rustboro", lambda a: a.game.flag("FLAG_VISITED_RUSTBORO_CITY"),
              [goto("MAP_RUSTBORO_CITY")]),
    Milestone("badge_stone", badges(1),
              [goto("MAP_RUSTBORO_CITY_GYM"),
               talk_s("MAP_RUSTBORO_CITY_GYM", "EventScript_Roxanne")],
              min_level=15, important=True),

    # -- Devon Goods, Mr. Briney, Dewford ------------------------------------------
    Milestone("goods_stolen", any_of(flag("FLAG_DEVON_GOODS_STOLEN"),
                                     flag("FLAG_RECOVERED_DEVON_GOODS")),
              [goto("MAP_RUSTBORO_CITY", 23, 22)]),
    Milestone("recover_goods", flag("FLAG_RECOVERED_DEVON_GOODS"),
              [goto("MAP_RUSTURF_TUNNEL"), talk_s("MAP_RUSTURF_TUNNEL", "EventScript_Grunt")],
              min_level=16),
    Milestone("pokenav", flag("FLAG_RECEIVED_POKENAV"),
              [goto("MAP_RUSTBORO_CITY", 30, 11)]),
    Milestone("sail_dewford", flag("FLAG_VISITED_DEWFORD_TOWN"),
              [goto("MAP_ROUTE104_MR_BRINEYS_HOUSE"),
               talk_s("MAP_ROUTE104_MR_BRINEYS_HOUSE", "EventScript_Briney")]),
    Milestone("badge_knuckle", badges(2),
              [goto("MAP_DEWFORD_TOWN_GYM"), talk_s("MAP_DEWFORD_TOWN_GYM", "EventScript_Brawly")],
              min_level=20, important=True),
    Milestone("steven_letter", flag("FLAG_DELIVERED_STEVEN_LETTER"),
              [goto("MAP_GRANITE_CAVE_STEVENS_ROOM"),
               talk_s("MAP_GRANITE_CAVE_STEVENS_ROOM", "EventScript_Steven")], min_level=21),
    Milestone("sail_slateport", flag("FLAG_VISITED_SLATEPORT_CITY"),
              [prefer("SLATEPORT"),
               unless(reachable("MAP_SLATEPORT_CITY"), goto("MAP_DEWFORD_TOWN"),
                      talk_s("MAP_DEWFORD_TOWN", "EventScript_Briney")),
               goto("MAP_SLATEPORT_CITY")]),

    # -- Slateport, Mauville ---------------------------------------------------------
    Milestone("shipyard", any_of(flag("FLAG_DOCK_REJECTED_DEVON_GOODS"),
                                 flag("FLAG_DELIVERED_DEVON_GOODS")),
              [goto("MAP_SLATEPORT_CITY_STERNS_SHIPYARD_1F"),
               talk_s("MAP_SLATEPORT_CITY_STERNS_SHIPYARD_1F", "EventScript_Dock")]),
    Milestone("deliver_goods", flag("FLAG_DELIVERED_DEVON_GOODS"),
              [goto("MAP_SLATEPORT_CITY_OCEANIC_MUSEUM_1F", 9, 7),
               goto("MAP_SLATEPORT_CITY_OCEANIC_MUSEUM_2F"),
               talk_s("MAP_SLATEPORT_CITY_OCEANIC_MUSEUM_2F", "EventScript_CaptStern")],
              min_level=24),
    Milestone("reach_mauville", flag("FLAG_VISITED_MAUVILLE_CITY"),
              [goto("MAP_MAUVILLE_CITY")]),
    Milestone("rock_smash", flag("FLAG_RECEIVED_HM_ROCK_SMASH"),
              [goto("MAP_MAUVILLE_CITY_HOUSE1"),
               talk_s("MAP_MAUVILLE_CITY_HOUSE1", "EventScript_RockSmashDude")]),
    Milestone("wally_mauville", flag("FLAG_DEFEATED_WALLY_MAUVILLE"),
              [answer("battle me", True), goto("MAP_MAUVILLE_CITY"),
               talk_s("MAP_MAUVILLE_CITY", "EventScript_Wally")], min_level=27),
    Milestone("badge_dynamo", badges(3),
              [goto("MAP_MAUVILLE_CITY_GYM"),
               call("goto_puzzle", "MAP_MAUVILLE_CITY_GYM", 5, 2),
               talk_s("MAP_MAUVILLE_CITY_GYM", "EventScript_Wattson")],
              min_level=27, important=True),

    # -- Rock Smash north, Meteor Falls, Mt. Chimney, Lavaridge -----------------------------
    Milestone("teach_rock_smash", lambda a: any(p.knows("MOVE_ROCK_SMASH") for p in a.game.party()),
              [call("teach", "ITEM_HM06")]),
    Milestone("meteor_falls", var_ge("VAR_METEOR_FALLS_STATE", 1),
              [goto("MAP_METEOR_FALLS_1F_1R", 14, 18)], min_level=30),
    Milestone("mt_chimney", flag("FLAG_DEFEATED_EVIL_TEAM_MT_CHIMNEY"),
              [unless(reachable("MAP_MT_CHIMNEY"),
                      goto("MAP_ROUTE112_CABLE_CAR_STATION"),
                      talk_s("MAP_ROUTE112_CABLE_CAR_STATION", "EventScript_Attendant")),
               goto("MAP_MT_CHIMNEY"), talk_s("MAP_MT_CHIMNEY", "EventScript_Maxie")],
              min_level=32, important=True),
    Milestone("reach_lavaridge", flag("FLAG_VISITED_LAVARIDGE_TOWN"),
              [goto("MAP_LAVARIDGE_TOWN")]),
    Milestone("badge_heat", badges(4),
              [goto("MAP_LAVARIDGE_TOWN_GYM_1F"),
               talk_s("MAP_LAVARIDGE_TOWN_GYM_1F", "EventScript_Flannery")],
              min_level=34, important=True),
]
