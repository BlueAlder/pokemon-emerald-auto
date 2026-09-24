"""Pokemon Emerald's critical path, from power-on to Hall of Fame.

Each milestone names what the game records when that beat is finished (a flag
or var set by the story script, a badge, an item) and the actions that get
there. Coordinates and object ids come from the decomp's map data; the
planner does all the walking.

Assumes the default player (Brendan); rival house maps are chosen by gender.
"""
from __future__ import annotations

from .route import (Milestone, all_of, any_of, badges, call, flag, goto, has_item, interact,
                    party_size, prefer, reachable, answer, trigger, talk, talk_s, trainer_beaten, unless,
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


def petalburg_gym(a):
    """Norman's gym is a chain of rooms; each room's exit door only opens once
    its trainer is beaten. Speed -> Confusion -> Strength -> Norman."""
    gym = "MAP_PETALBURG_CITY_GYM"
    rooms = [((1, 105), None),                                    # entrance -> SPEED
             ((1, 79), "PetalburgCity_Gym_EventScript_Randall"),   # SPEED -> CONFUSION
             ((7, 40), "PetalburgCity_Gym_EventScript_Parker"),    # CONFUSION -> STRENGTH
             ((7, 14), "PetalburgCity_Gym_EventScript_Jody")]      # STRENGTH -> NORMAN
    for (dx, dy), trainer in rooms:
        x, y = a.game.pos()
        if a.game.map_id() == gym and y < dy:
            continue                      # already past this door (rooms go north)
        if trainer:
            from .route import object_id
            a.talk(gym, object_id(gym, trainer))
        a.interact(gym, dx, dy, "up")
    from .route import object_id
    a.talk(gym, object_id(gym, "PetalburgCity_Gym_EventScript_Norman"))


E4_ROOMS = ["MAP_EVER_GRANDE_CITY_SIDNEYS_ROOM", "MAP_EVER_GRANDE_CITY_PHOEBES_ROOM",
            "MAP_EVER_GRANDE_CITY_GLACIAS_ROOM", "MAP_EVER_GRANDE_CITY_DRAKES_ROOM",
            "MAP_EVER_GRANDE_CITY_CHAMPIONS_ROOM"]


def e4_room(target: str):
    """Walk to an Elite Four room one room at a time. Each cleared room's exit
    is opened by a script on entry (setmetatile), which only the live map
    shows, so every hop is planned from inside the previous room."""
    def act(a):
        here = a.game.map_id()
        start = E4_ROOMS.index(here) + 1 if here in E4_ROOMS else 0
        for room in E4_ROOMS[start:E4_ROOMS.index(target) + 1]:
            a.goto(room)
            a.pump()
    act.__name__ = f"e4_room {target}"
    return act


SOOTOPOLIS = "MAP_SOOTOPOLIS_CITY"
LEAGUE = "MAP_EVER_GRANDE_CITY_POKEMON_LEAGUE_1F"
VICTORY_ROAD = ["MAP_VICTORY_ROAD_1F", "MAP_VICTORY_ROAD_B1F"]

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
    Milestone("get_strength", flag("FLAG_RECEIVED_HM_STRENGTH"),
              [goto("MAP_RUSTURF_TUNNEL"), interact("MAP_RUSTURF_TUNNEL", 24, 5)],
              hint="smash the rocks in Rusturf Tunnel so Wanda's boyfriend gives HM04 Strength"),
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

    # -- an HM carrier, Norman, Surf -------------------------------------------------------
    Milestone("catch_marill", lambda a: any(p.species_name in ("MARILL", "AZUMARILL")
                                            for p in a.game.party()),
              [call("catch", "SPECIES_MARILL", ["MAP_ROUTE112", "MAP_ROUTE104", "MAP_ROUTE120"])],
              hint="catch a Marill to carry HM moves"),
    Milestone("teach_strength", lambda a: any(p.knows("MOVE_STRENGTH") for p in a.game.party()),
              [call("teach", "ITEM_HM04", ["AZUMARILL", "MARILL"])]),
    Milestone("badge_balance", badges(5),
              [goto("MAP_PETALBURG_CITY_GYM"), petalburg_gym],
              min_level=40, important=True, hint="beat Norman at the Petalburg Gym"),
    Milestone("surf", flag("FLAG_RECEIVED_HM_SURF"),
              [goto("MAP_PETALBURG_CITY"), goto("MAP_PETALBURG_CITY_WALLYS_HOUSE")],
              hint="get HM03 Surf from Wally's father in Petalburg"),
    Milestone("teach_surf", lambda a: any(p.knows("MOVE_SURF") for p in a.game.party()),
              [call("teach", "ITEM_HM03", ["SWAMPERT", "MARSHTOMP"], "MOVE_WATER_GUN")]),

    # -- Route 119, the Weather Institute, Fortree ---------------------------------------------
    Milestone("weather_institute", var_ge("VAR_WEATHER_INSTITUTE_STATE", 1),
              [goto("MAP_ROUTE119_WEATHER_INSTITUTE_2F"),
               talk_s("MAP_ROUTE119_WEATHER_INSTITUTE_2F", "EventScript_Shelly")],
              min_level=42, important=True, hint="drive Team Aqua out of the Weather Institute"),
    Milestone("reach_fortree", flag("FLAG_VISITED_FORTREE_CITY"), [goto("MAP_FORTREE_CITY")]),
    Milestone("devon_scope", flag("FLAG_RECEIVED_DEVON_SCOPE"),
              [goto("MAP_ROUTE120"), talk_s("MAP_ROUTE120", "Route120_EventScript_Steven")],
              hint="meet Steven on the Route 120 bridge and get the Devon Scope"),
    Milestone("fortree_kecleon", flag("FLAG_KECLEON_FLED_FORTREE"),
              [goto("MAP_FORTREE_CITY"), talk_s("MAP_FORTREE_CITY", "FortreeCity_EventScript_Kecleon")],
              hint="reveal the invisible Kecleon blocking the Fortree Gym"),
    Milestone("badge_feather", badges(6),
              [goto("MAP_FORTREE_CITY_GYM"), call("fortree_gym")],
              min_level=44, important=True, hint="beat Winona at the Fortree Gym"),

    # -- Lilycove, Mt. Pyre, the Magma Hideout, the harbor, the Aqua Hideout -------------
    Milestone("reach_lilycove", flag("FLAG_VISITED_LILYCOVE_CITY"), [goto("MAP_LILYCOVE_CITY")]),
    Milestone("mt_pyre_summit", var_ge("VAR_MT_PYRE_STATE", 1),
              [goto("MAP_MT_PYRE_SUMMIT", 23, 7)], min_level=46, important=True,
              hint="climb Mt. Pyre and stop Team Aqua at the summit"),
    Milestone("magma_emblem", has_item("ITEM_MAGMA_EMBLEM"),
              [goto("MAP_MT_PYRE_SUMMIT"), talk_s("MAP_MT_PYRE_SUMMIT", "EventScript_OldLady")],
              hint="get the Magma Emblem from the old lady at Mt. Pyre's summit"),
    Milestone("open_magma_hideout", var_ge("VAR_JAGGED_PASS_STATE", 2),
              [goto("MAP_JAGGED_PASS"),
               trigger("MAP_JAGGED_PASS", "JaggedPass_EventScript_OpenMagmaHideout")],
              hint="use the Magma Emblem to open the hidden entrance on Jagged Pass"),
    Milestone("magma_hideout", flag("FLAG_GROUDON_AWAKENED_MAGMA_HIDEOUT"),
              [goto("MAP_MAGMA_HIDEOUT_4F"), talk_s("MAP_MAGMA_HIDEOUT_4F", "EventScript_Maxie")],
              min_level=48, important=True, hint="stop Maxie deep inside the Magma Hideout"),
    Milestone("slateport_harbor", var_ge("VAR_SLATEPORT_HARBOR_STATE", 2),
              [unless(lambda a: a.game.var("VAR_SLATEPORT_CITY_STATE") >= 2,
                      goto("MAP_SLATEPORT_CITY"),
                      talk_s("MAP_SLATEPORT_CITY", "SlateportCity_EventScript_CaptStern")),
               goto("MAP_SLATEPORT_CITY_HARBOR", 8, 12)],
              hint="see Team Aqua steal the submarine at Slateport Harbor"),
    Milestone("aqua_hideout", flag("FLAG_HIDE_LILYCOVE_CITY_AQUA_GRUNTS"),
              [goto("MAP_AQUA_HIDEOUT_B2F"), goto("MAP_AQUA_HIDEOUT_B2F", 28, 17),
               talk_s("MAP_AQUA_HIDEOUT_B2F", "EventScript_Matt")],
              min_level=48, important=True, hint="clear the Team Aqua Hideout in Lilycove"),
    Milestone("reach_mossdeep", flag("FLAG_VISITED_MOSSDEEP_CITY"),
              [goto("MAP_MOSSDEEP_CITY"), trigger("MAP_MOSSDEEP_CITY", "VisitedMossdeep")]),

    # -- Mossdeep, the Space Center, Dive, Seafloor Cavern ---------------------------------
    Milestone("badge_mind", badges(7),
              [call("rotating_tile_gym", "MAP_MOSSDEEP_CITY_GYM", "EventScript_TateAndLiza", 7)],
              min_level=54, team_level=42, important=True,
              hint="beat Tate and Liza at the Mossdeep Gym"),
    Milestone("space_center", flag("FLAG_DEFEATED_MAGMA_SPACE_CENTER"),
              [goto("MAP_MOSSDEEP_CITY_SPACE_CENTER_2F"),
               talk_s("MAP_MOSSDEEP_CITY_SPACE_CENTER_2F", "SpaceCenter_2F_EventScript_Steven")],
              min_level=55, important=True, hint="stop Team Magma at the Mossdeep Space Center"),
    Milestone("dive", flag("FLAG_RECEIVED_HM_DIVE"),
              [goto("MAP_MOSSDEEP_CITY_STEVENS_HOUSE"),
               talk_s("MAP_MOSSDEEP_CITY_STEVENS_HOUSE", "StevensHouse_EventScript_Steven")],
              hint="get HM08 Dive from Steven in Mossdeep"),
    Milestone("teach_dive", lambda a: any(p.knows("MOVE_DIVE") for p in a.game.party()),
              [call("teach", "ITEM_HM08", ["AZUMARILL", "MARILL"])]),
    Milestone("seafloor_cavern", var_ge("VAR_SEAFLOOR_CAVERN_STATE", 1),
              [goto("MAP_SEAFLOOR_CAVERN_ROOM9"),
               trigger("MAP_SEAFLOOR_CAVERN_ROOM9", "SeafloorCavern_Room9_EventScript_ArchieAwakenKyogre")],
              min_level=56, important=True, hint="stop Archie in the Seafloor Cavern"),

    # -- Sootopolis, the Cave of Origin, Sky Pillar, Rayquaza ------------------------------
    Milestone("sootopolis", var_ge("VAR_SOOTOPOLIS_CITY_STATE", 2), [goto(SOOTOPOLIS)],
              hint="dive to Sootopolis City, where Groudon and Kyogre are fighting"),
    Milestone("steven_cave", flag("FLAG_STEVEN_GUIDES_TO_CAVE_OF_ORIGIN"),
              [goto(SOOTOPOLIS), talk_s(SOOTOPOLIS, "SootopolisCity_EventScript_Steven")],
              hint="follow Steven to the Cave of Origin"),
    Milestone("wallace_cave", flag("FLAG_WALLACE_GOES_TO_SKY_PILLAR"),
              [prefer("SKY PILLAR"), goto("MAP_CAVE_OF_ORIGIN_B1F"),
               talk_s("MAP_CAVE_OF_ORIGIN_B1F", "EventScript_Wallace")],
              hint="tell Wallace in the Cave of Origin that Rayquaza is at the Sky Pillar"),
    Milestone("sky_pillar_open", var_ge("VAR_SOOTOPOLIS_CITY_STATE", 4),
              [goto("MAP_SKY_PILLAR_OUTSIDE")], hint="meet Wallace outside the Sky Pillar"),
    Milestone("rayquaza", var_ge("VAR_SKY_PILLAR_STATE", 1),
              [goto("MAP_SKY_PILLAR_OUTSIDE"), goto("MAP_SKY_PILLAR_TOP"),
               trigger("MAP_SKY_PILLAR_TOP", "AwakenRayquaza")],
              min_level=56, hint="climb the Sky Pillar and wake Rayquaza"),
    Milestone("rayquaza_calms", var_ge("VAR_SKY_PILLAR_STATE", 2), [goto(SOOTOPOLIS)],
              hint="return to Sootopolis where Rayquaza stops the fight"),
    Milestone("maxie_archie", flag("FLAG_SOOTOPOLIS_ARCHIE_MAXIE_LEAVE"),
              [goto(SOOTOPOLIS), talk_s(SOOTOPOLIS, "SootopolisCity_EventScript_Maxie"),
               talk_s(SOOTOPOLIS, "SootopolisCity_EventScript_Archie")],
              hint="talk to Maxie and Archie in Sootopolis"),
    Milestone("waterfall", flag("FLAG_RECEIVED_HM_WATERFALL"),
              [goto(SOOTOPOLIS), talk_s(SOOTOPOLIS, "SootopolisCity_EventScript_Wallace")],
              hint="get HM07 Waterfall from Wallace in Sootopolis"),
    Milestone("teach_waterfall", lambda a: any(p.knows("MOVE_WATERFALL") for p in a.game.party()),
              [call("teach", "ITEM_HM07", ["AZUMARILL", "MARILL", "SWAMPERT"])]),
    Milestone("badge_rain", badges(8),
              [goto("MAP_SOOTOPOLIS_CITY_GYM_1F"),
               call("ice_gym", "MAP_SOOTOPOLIS_CITY_GYM_1F", "EventScript_Juan", 8)],
              min_level=58, team_level=50, important=True,
              hint="crack the ice floors of the Sootopolis Gym and beat Juan"),

    # -- Ever Grande, Victory Road, the Elite Four ------------------------------------------
    Milestone("reach_ever_grande", flag("FLAG_VISITED_EVER_GRANDE_CITY"),
              [goto("MAP_EVER_GRANDE_CITY"),
               trigger("MAP_EVER_GRANDE_CITY", "SetVisitedEverGrande")],
              hint="climb the waterfall on Route 128 to Ever Grande City"),
    Milestone("victory_road", flag("FLAG_DEFEATED_WALLY_VICTORY_ROAD"),
              [goto("MAP_VICTORY_ROAD_1F"),
               trigger("MAP_VICTORY_ROAD_1F", "WallyBattleTrigger1")],
              min_level=60, important=True, hint="beat Wally at the entrance of Victory Road"),
    # Five fights with no Pokemon Center: two Pokemon run out of PP. Recruit
    # two more from Victory Road (Hariyama catches easily) and train them.
    Milestone("e4_team", party_size(5),
              [call("shop", {"ITEM_ULTRA_BALL": 15}),
               call("catch", "SPECIES_HARIYAMA", VICTORY_ROAD),
               call("catch", "SPECIES_GOLBAT", VICTORY_ROAD)],
              hint="catch a Hariyama and a Golbat in Victory Road for the Elite Four"),
    # More PP for five fights in a row: Water Pulse (20 PP) over Take Down,
    # Facade (Huge Power) over Hydro Pump's 5 PP.
    Milestone("e4_moves", lambda a: all(
        not a.game.has_item(tm) or any(p.knows(mv) for p in a.game.party())
        for tm, mv in (("ITEM_TM03", "MOVE_WATER_PULSE"), ("ITEM_TM42", "MOVE_FACADE"),
                       ("ITEM_TM39", "MOVE_ROCK_TOMB"), ("ITEM_TM40", "MOVE_AERIAL_ACE"))),
              [call("teach", "ITEM_TM03", ["SWAMPERT"], "MOVE_TAKE_DOWN"),
               call("teach", "ITEM_TM42", ["AZUMARILL"], "MOVE_HYDRO_PUMP"),
               call("teach", "ITEM_TM39", ["HARIYAMA"], "MOVE_WHIRLWIND"),
               call("teach", "ITEM_TM40", ["GOLBAT"])]),
    Milestone("enter_league", flag("FLAG_ENTERED_ELITE_FOUR"),
              [goto(LEAGUE), call("league_supplies"),
               talk_s(LEAGUE, "PokemonLeague_1F_EventScript_DoorGuard")],
              min_level=70, team_level=60, team_size=4, important=True,
              hint="cross Victory Road and show the guards all eight badges"),
    Milestone("sidney", flag("FLAG_DEFEATED_ELITE_4_SIDNEY"),
              [call("heal_with_items"), e4_room("MAP_EVER_GRANDE_CITY_SIDNEYS_ROOM"),
               talk_s("MAP_EVER_GRANDE_CITY_SIDNEYS_ROOM", "EventScript_Sidney")],
              heal_first=False, important=True, hint="beat Sidney of the Elite Four"),
    Milestone("phoebe", flag("FLAG_DEFEATED_ELITE_4_PHOEBE"),
              [call("heal_with_items"), e4_room("MAP_EVER_GRANDE_CITY_PHOEBES_ROOM"),
               talk_s("MAP_EVER_GRANDE_CITY_PHOEBES_ROOM", "EventScript_Phoebe")],
              heal_first=False, important=True, hint="beat Phoebe of the Elite Four"),
    Milestone("glacia", flag("FLAG_DEFEATED_ELITE_4_GLACIA"),
              [call("heal_with_items"), e4_room("MAP_EVER_GRANDE_CITY_GLACIAS_ROOM"),
               talk_s("MAP_EVER_GRANDE_CITY_GLACIAS_ROOM", "EventScript_Glacia")],
              heal_first=False, important=True, hint="beat Glacia of the Elite Four"),
    Milestone("drake", flag("FLAG_DEFEATED_ELITE_4_DRAKE"),
              [call("heal_with_items"), e4_room("MAP_EVER_GRANDE_CITY_DRAKES_ROOM"),
               talk_s("MAP_EVER_GRANDE_CITY_DRAKES_ROOM", "EventScript_Drake")],
              heal_first=False, important=True, hint="beat Drake of the Elite Four"),
    Milestone("champion", flag("FLAG_SYS_GAME_CLEAR"),
              [call("heal_with_items"), e4_room("MAP_EVER_GRANDE_CITY_CHAMPIONS_ROOM")],
              heal_first=False, important=True, hint="beat Champion Wallace"),
]
