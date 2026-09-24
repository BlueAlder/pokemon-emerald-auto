"""Menu drivers shared by the field and battle code: the Bag and Marts.

Both read the menu's own cursor state from RAM (gBagPosition, sShopData) and
press keys until it matches, so a dropped or doubled input self-corrects.
"""
from __future__ import annotations

import logging
import struct

from .controller import Stuck
from .symbols import const, const_names, symbols

log = logging.getLogger("pokeauto")
S = symbols()

BAG_POCKETS = ["items", "balls", "tmhm", "berries", "key"]      # bag UI order


def bag_pos(emu) -> dict:
    raw = emu.read(S["gBagPosition"], 28)
    cursor = struct.unpack_from("<5H", raw, 8)
    scroll = struct.unpack_from("<5H", raw, 18)
    return {"pocket": raw[5], "index": [c + s for c, s in zip(cursor, scroll)]}


def pocket_of(game, item_id: int) -> str:
    for p in BAG_POCKETS:
        if item_id in game.bag_order(p):
            return p
    raise Stuck(f"item {const_names()['ITEM_'].get(item_id, item_id)} is not in the bag")


def _bag_ready(ctl, timeout: int = 120) -> None:
    """Wait until the bag accepts input (not opening, not switching pockets)."""
    for _ in range(timeout):
        tasks = ctl.game.active_tasks()
        if "BagMenu" in S.name_at(ctl.game.callback2()) and \
                "Task_BagMenu_HandleInput" in tasks and "Task_SwitchBagPocket" not in tasks:
            return
        ctl.idle(2)


def bag_select(ctl, item_id: int) -> None:
    """With the bag open, move to item_id and press A (opens its context menu)."""
    game, emu = ctl.game, ctl.emu
    _bag_ready(ctl)
    pocket = pocket_of(game, item_id)
    pidx = BAG_POCKETS.index(pocket)
    for _ in range(12):
        cur = bag_pos(emu)["pocket"]
        if cur == pidx:
            break
        ctl.press("RIGHT" if cur < pidx else "LEFT", release=4)
        _bag_ready(ctl)
    _bag_ready(ctl)
    want = game.bag_order(pocket).index(item_id)
    for _ in range(80):
        cur = bag_pos(emu)["index"][pidx]
        if cur == want:
            break
        ctl.press("DOWN" if cur < want else "UP", release=6)
    ctl.press("A", release=16)


def buy(ctl, clerk_talk, wants: dict[str, int]) -> None:
    """Buy items at a Mart. `clerk_talk` is a callable that talks to the clerk.

    wants: {"ITEM_SUPER_POTION": 10, ...}; quantities are capped by money.
    """
    game, emu = ctl.game, ctl.emu
    clerk_talk_started = False

    def shop_items() -> list[int]:
        info = emu.read(S["sMartInfo"], 16)
        ptr, count = struct.unpack_from("<I", info, 8)[0], struct.unpack_from("<H", info, 12)[0]
        return [emu.u16(ptr + i * 2) for i in range(count)]

    todo = [(const(k), v) for k, v in wants.items() if v > 0]
    for _ in range(600):
        tasks = game.active_tasks()
        if not clerk_talk_started:
            clerk_talk()                     # opens "How may I serve you?"
            clerk_talk_started = True
            continue
        if "Task_ShopMenu" in tasks:
            if todo:
                ctl._menu_select(0)          # BUY
            else:
                ctl.press("B", release=12)   # QUIT
            continue
        if "Task_BuyMenu" in tasks:
            items = shop_items()
            while todo and todo[0][0] not in items:
                log.info("SHOP does not sell %s", const_names()["ITEM_"].get(todo[0][0]))
                todo.pop(0)
            if not todo:
                ctl.press("B", release=16)
                continue
            want = items.index(todo[0][0])
            data = emu.u32(S["sShopData"])
            row, scroll = struct.unpack_from("<HH", emu.read(data + 0x2006, 4))
            cur = row + scroll
            if cur != want:
                ctl.press("DOWN" if cur < want else "UP", release=6)
            else:
                ctl.press("A", release=16)
            continue
        if "Task_BuyHowManyDialogueHandleInput" in tasks:
            d = game.task_data("Task_BuyHowManyDialogueHandleInput")
            qty = todo[0][1]
            if d[1] < qty:
                ctl.press("RIGHT" if qty - d[1] >= 10 else "UP", release=5)
                d2 = game.task_data("Task_BuyHowManyDialogueHandleInput")
                if d2 and d2[1] <= d[1]:          # hit the money / bag cap (it wraps to 1)
                    todo[0] = (todo[0][0], max(1, d[1]))
            elif d[1] > qty:
                ctl.press("LEFT" if d[1] - qty >= 10 else "DOWN", release=5)
            else:
                log.info("SHOP buying %d x %s", qty, const_names()["ITEM_"].get(todo[0][0]))
                ctl.press("A", release=16)
                todo.pop(0)
            continue
        if any("YesNo" in t or "YesOrNo" in t for t in tasks):
            ctl._menu_select(0)
            continue
        m = game.mode()
        if m.kind == "overworld" and ctl.free():
            return
        ctl.press("A", release=8)
    raise Stuck("shopping did not finish")


SUMMARY_MOVE_CURSOR = 0x40C6      # PokemonSummaryScreenData.firstMoveIndex


def summary_select_move(ctl, slot: int) -> None:
    """On the 'forget which move?' summary screen, move the cursor to `slot`
    (0-3 = known moves, 4 = the new move) and confirm."""
    emu = ctl.emu
    for _ in range(60):
        ptr = emu.u32(S["sMonSummaryScreen"])
        if 0x02000000 <= ptr < 0x02040000:
            break
        ctl.idle(4)
    ctl.idle(30)                               # let the move page finish sliding in
    for _ in range(12):
        ptr = emu.u32(S["sMonSummaryScreen"])
        cur = emu.u8(ptr + SUMMARY_MOVE_CURSOR)
        if cur == slot:
            break
        ctl.press("DOWN" if cur < slot else "UP", release=8)
    ctl.press("A", release=30)
