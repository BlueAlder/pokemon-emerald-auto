-- Runs lua/bridge.lua against stubbed mGBA globals to verify the command
-- handler and the frame-accurate input scheduler.
local sent, logged = {}, {}
local frame = 100
local keys_history = {}

console = { log = function(_, m) logged[#logged+1] = m end }
C = { GBA_KEY = {A=0,B=1,SELECT=2,START=3,RIGHT=4,LEFT=5,UP=6,DOWN=7,R=8,L=9} }

emu = {
  currentFrame = function() return frame end,
  getGameCode  = function() return "BPEE" end,
  getGameTitle = function() return "POKEMON EMER" end,
  romSize      = function() return 16777216 end,
  readRange    = function(_, addr, len)
                   local t = {}
                   for i = 0, len-1 do t[#t+1] = string.char((addr + i) % 256) end
                   return table.concat(t)
                 end,
  setKeys      = function(_, m) keys_history[#keys_history+1] = m end,
  saveStateSlot= function() return true end,
  loadStateSlot= function() return true end,
  screenshot   = function() end,
}

local frame_cbs, key_cbs = {}, {}
callbacks = { add = function(_, name, fn)
  if name == "keysRead" then key_cbs[#key_cbs+1] = fn else frame_cbs[#frame_cbs+1] = fn end
end }

local fake_client = {
  send = function(_, d) sent[#sent+1] = d:gsub("\n$", ""); return #d end,
  add  = function() end,
}
socket = {
  ERRORS = { AGAIN = "again" },
  bind = function()
    return { listen = function() return 0 end, add = function() end,
             accept = function() return fake_client end }
  end,
}

dofile("lua/bridge.lua")

-- Reach into the chunk via its exposed behaviour: simulate a connection by
-- invoking the accept path, then feed commands.
local env = _G
local fails = 0
local function check(label, got, want)
  if got == want then print(("  ok   %s: %s"):format(label, tostring(got)))
  else fails = fails + 1
       print(("  FAIL %s: got %s want %s"):format(label, tostring(got), tostring(want))) end
end

print("bridge.lua behavioural test")
print("startup log: " .. (logged[1] or "(none)"))
check("bound and listening", (logged[1] or ""):match("listening") ~= nil, true)

-- Drive one frame tick so the keysRead callback is registered and working.
local tick = key_cbs[1]
check("keysRead callback registered", tick ~= nil, true)

-- Without any queued input the mask must stay 0.
frame = frame + 1; tick()
check("idle mask is zero", keys_history[#keys_history], 0)


local B = pokeauto_bridge
B.set_client(fake_client)

local function last() return sent[#sent] end
local function cmd(line) B.handle(line); return last() end

print("\n-- command handler --")
check("PING",  cmd("PING"), "PONG")
check("INFO (title last, has a space)", cmd("INFO"),
      "BPEE 16777216 " .. frame .. " POKEMON EMER")
check("FRAME", cmd("FRAME"), tostring(frame))
check("READ 4 bytes", cmd("READ 02000000 4"), "00010203")
check("READM two ranges", cmd("READM 02000000:2,02000004:3"), "0001|040506")
check("bad args rejected", cmd("READ nothex"), "ERR bad_args")
check("unknown command",  cmd("ZZZ"), "ERR unknown_command ZZZ")
check("SHOT needs a path", cmd("SHOT"), "ERR bad_args")

print("\n-- input scheduler --")
-- PRESS A with a 3-frame hold and a 2-frame release gap.
local reply = cmd("PRESS 1 3 2")
check("PRESS acknowledged with target frame", reply, "OK " .. (frame + 5))
check("one press queued", B.pending(), 1)

local seen = {}
for _ = 1, 6 do
  frame = frame + 1
  tick()
  seen[#seen+1] = keys_history[#keys_history]
end
-- 3 frames held (mask 1), then 2 frames released (mask 0), then idle.
check("frame 1 held",    seen[1], 1)
check("frame 2 held",    seen[2], 1)
check("frame 3 held",    seen[3], 1)
check("frame 4 released",seen[4], 0)
check("frame 5 released",seen[5], 0)
check("frame 6 idle",    seen[6], 0)
check("queue drained",   B.pending(), 0)

-- Two presses queue up and run back to back without overlapping.
cmd("PRESS 32 2 1")   -- LEFT
cmd("PRESS 8 2 1")    -- START
local combo = {}
for _ = 1, 6 do frame = frame + 1; tick(); combo[#combo+1] = keys_history[#keys_history] end
check("queued presses run in order", table.concat(combo, ","), "32,32,0,8,8,0")

print("\n-- RUN: frame-synchronous holds --")
local function frames(n)
  local out = {}
  for _ = 1, n do frame = frame + 1; tick(); out[#out+1] = keys_history[#keys_history] end
  return table.concat(out, ",")
end
-- Every frame of a RUN is held, including the last (no release gap).
local before = #sent
B.handle("RUN 32 3")
check("RUN replies only after its frames", #sent, before)
check("RUN holds all 3 frames", frames(3), "32,32,32")
frames(1)
check("RUN replied once done", last():match("^OK %d+$") ~= nil, true)

-- A one-frame RUN presses for that frame (it used to press nothing).
B.handle("RUN 64 1")
check("RUN of 1 frame is held", frames(1), "64")

-- Back-to-back RUNs with Python reads in between: the hold carries over the
-- gap instead of flickering (Emerald turns in place on a flickering direction).
B.handle("RUN 16 1")
check("hold carries over the gap between RUNs", frames(4), "16,16,16,16")
B.handle("RUN 16 1")
check("next RUN continues the hold", frames(1), "16")

-- RUN 0 releases at once.
B.handle("RUN 0 2")
check("RUN 0 releases", frames(3), "0,0,0")

-- A forgotten hold expires after 12 frames.
B.handle("RUN 128 1")
local tail = frames(15)
check("carried-over hold expires", tail, "128,128,128,128,128,128,128,128,128,128,128,128,128,0,0")

-- Any other input command replaces a carried-over hold.
B.handle("RUN 128 1")
frames(2)
B.handle("PRESS 1 2 1")
check("PRESS takes over from a carried hold", frames(4), "1,1,0,0")

print(("\n%d check(s) failed"):format(fails))
os.exit(fails == 0 and 0 or 1)
