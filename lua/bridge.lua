-- poke_auto :: mGBA <-> Python bridge
--
-- Load via mGBA: Tools > Scripting... > File > Load script... > this file.
-- Speaks a tiny line protocol over TCP so that ALL game knowledge lives in
-- Python. Lua stays deliberately dumb: it reads bytes, presses buttons, and
-- reports frames. It does no JSON and knows nothing about Pokemon.
--
-- Protocol (newline-delimited ASCII, one reply line per command):
--   PING                      -> PONG
--   INFO                      -> <gamecode> <romsize> <frame> <title>
--                                (title last: it contains a space)
--   FRAME                     -> <frame>
--   READ <hexaddr> <len>      -> <hexbytes>
--   READM <a>:<l>,<a>:<l>,... -> <hexbytes>|<hexbytes>|...
--   PRESS <mask> <hold> <gap> -> OK <frame_when_done>
--   HOLD <mask> <frames>      -> OK <frame_when_done>   (no release gap)
--   IDLE <frames>             -> OK <frame_when_done>   (advance, no input)
--   RUN <mask> <frames>       -> OK <frame>  sent only AFTER the frames have
--                                elapsed, so the client is frame-synchronous
--   CAPS                      -> space-separated list of supported commands
--   STATE <slot>              -> OK      (save state to slot)
--   LOAD <slot>               -> OK      (load state from slot)
--   SHOT <path>               -> OK      (screenshot; for humans, not the model)
--   ERR ...                   -> any command that fails

local PORT = 8888

local server = nil
local client = nil
local rxbuf = ""

-- Input scheduler ----------------------------------------------------------
-- Buttons must be held for several frames to register, then released for a
-- few more or the game reads them as one long press. We run a small state
-- machine advanced exactly once per frame.
local queue = {}
local cur = nil
local phase = "idle"
local left = 0
local mask_now = 0
local last_frame = -1
local run_pending = false   -- a RUN command is waiting for its frames to pass

local function tohex(s)
  return (s:gsub(".", function(c) return string.format("%02x", c:byte()) end))
end

local function reply(line)
  if client then
    local ok, err = client:send(line .. "\n")
    if not ok then
      console:log("poke_auto: send failed: " .. tostring(err))
    end
  end
end

local function queue_done_frame()
  local f = emu:currentFrame()
  local total = 0
  if cur then total = left + (phase == "hold" and cur.gap or 0) end
  for _, item in ipairs(queue) do total = total + item.hold + item.gap end
  return f + total
end

local function enqueue(mask, hold, gap)
  table.insert(queue, { mask = mask, hold = hold, gap = gap })
  return queue_done_frame()
end

local function advance_input()
  if cur == nil then
    if #queue == 0 then
      mask_now = 0
      return
    end
    cur = table.remove(queue, 1)
    phase = "hold"
    left = cur.hold
  end

  if phase == "hold" then
    mask_now = cur.mask
    left = left - 1
    if left <= 0 then
      phase = "gap"
      left = cur.gap
      if left <= 0 then cur = nil; phase = "idle"; mask_now = 0 end
    end
  elseif phase == "gap" then
    mask_now = 0
    left = left - 1
    if left <= 0 then cur = nil; phase = "idle" end
  else
    mask_now = 0
  end
end

-- Command handling ---------------------------------------------------------

local function parse_ranges(spec)
  local out = {}
  for chunk in spec:gmatch("[^,]+") do
    local a, l = chunk:match("^(%x+):(%d+)$")
    if not a then return nil end
    table.insert(out, { addr = tonumber(a, 16), len = tonumber(l) })
  end
  if #out == 0 then return nil end
  return out
end

local function handle(line)
  local cmd, rest = line:match("^(%u+)%s*(.*)$")
  if not cmd then reply("ERR bad_command"); return end

  if cmd == "PING" then
    reply("PONG")

  elseif cmd == "INFO" then
    if not emu then reply("ERR no_game"); return end
    -- Title goes last because it contains a space ("POKEMON EMER").
    local title = (emu:getGameTitle() or "?"):gsub("%s+$", "")
    reply(string.format("%s %d %d %s",
      emu:getGameCode() or "????", emu:romSize(), emu:currentFrame(), title))

  elseif cmd == "FRAME" then
    reply(tostring(emu:currentFrame()))

  elseif cmd == "READ" then
    local a, l = rest:match("^(%x+)%s+(%d+)$")
    if not a then reply("ERR bad_args"); return end
    local ok, data = pcall(function()
      return emu:readRange(tonumber(a, 16), tonumber(l))
    end)
    if ok and data then reply(tohex(data)) else reply("ERR read_failed") end

  elseif cmd == "READM" then
    local ranges = parse_ranges(rest)
    if not ranges then reply("ERR bad_args"); return end
    local parts = {}
    for _, r in ipairs(ranges) do
      local ok, data = pcall(function() return emu:readRange(r.addr, r.len) end)
      table.insert(parts, (ok and data) and tohex(data) or "")
    end
    reply(table.concat(parts, "|"))

  elseif cmd == "PRESS" then
    local m, h, g = rest:match("^(%d+)%s+(%d+)%s+(%d+)$")
    if not m then reply("ERR bad_args"); return end
    reply("OK " .. tostring(enqueue(tonumber(m), tonumber(h), tonumber(g))))

  elseif cmd == "HOLD" then
    local m, f = rest:match("^(%d+)%s+(%d+)$")
    if not m then reply("ERR bad_args"); return end
    reply("OK " .. tostring(enqueue(tonumber(m), tonumber(f), 0)))

  elseif cmd == "IDLE" then
    local f = rest:match("^(%d+)$")
    if not f then reply("ERR bad_args"); return end
    reply("OK " .. tostring(enqueue(0, tonumber(f), 0)))

  elseif cmd == "RUN" then
    local m, f = rest:match("^(%d+)%s+(%d+)$")
    if not m then reply("ERR bad_args"); return end
    enqueue(tonumber(m), math.max(1, tonumber(f)), 0)
    run_pending = true          -- replied to from the frame callback

  elseif cmd == "CAPS" then
    reply("PING INFO FRAME READ READM PRESS HOLD IDLE RUN STATE LOAD SHOT CAPS")

  elseif cmd == "STATE" then
    local s = tonumber(rest)
    if not s then reply("ERR bad_args"); return end
    reply(emu:saveStateSlot(s) and "OK" or "ERR savestate_failed")

  elseif cmd == "LOAD" then
    local s = tonumber(rest)
    if not s then reply("ERR bad_args"); return end
    reply(emu:loadStateSlot(s) and "OK" or "ERR loadstate_failed")

  elseif cmd == "SHOT" then
    if rest == "" then reply("ERR bad_args"); return end
    emu:screenshot(rest)
    reply("OK")

  else
    reply("ERR unknown_command " .. cmd)
  end
end

local function drain()
  while true do
    local nl = rxbuf:find("\n")
    if not nl then break end
    local line = rxbuf:sub(1, nl - 1):gsub("\r$", "")
    rxbuf = rxbuf:sub(nl + 1)
    if #line > 0 then
      local ok, err = pcall(handle, line)
      if not ok then
        console:log("poke_auto: handler error: " .. tostring(err))
        reply("ERR exception")
      end
    end
  end
end

local function on_client_data()
  while client do
    local data, err = client:receive(4096)
    if data == nil then
      -- AGAIN simply means "nothing more right now".
      if err and err ~= socket.ERRORS.AGAIN then
        console:log("poke_auto: client closed (" .. tostring(err) .. ")")
        client = nil
      end
      break
    end
    if #data == 0 then break end
    rxbuf = rxbuf .. data
  end
  drain()
end

local function on_accept()
  local sock, err = server:accept()
  if not sock then
    console:log("poke_auto: accept failed: " .. tostring(err))
    return
  end
  if client then
    console:log("poke_auto: replacing existing client")
  end
  client = sock
  rxbuf = ""
  queue = {}; cur = nil; phase = "idle"; left = 0; mask_now = 0
  run_pending = false
  client:add("received", on_client_data)
  client:add("error", function() client = nil end)
  console:log("poke_auto: agent connected")
end

-- Wiring -------------------------------------------------------------------

callbacks:add("keysRead", function()
  local f = emu:currentFrame()
  if f ~= last_frame then
    last_frame = f
    if run_pending and cur == nil and #queue == 0 then
      run_pending = false
      reply("OK " .. tostring(f))
    end
    advance_input()
  end
  emu:setKeys(mask_now)
end)

server = socket.bind(nil, PORT)
if not server then
  console:log("poke_auto: could not bind port " .. PORT .. " (already running?)")
else
  server:listen()
  server:add("received", on_accept)
  console:log("poke_auto: bridge listening on 127.0.0.1:" .. PORT)
end

-- Exposed only so tests/test_bridge_lua.lua can drive the command handler and
-- the input scheduler directly. Nothing at runtime reads this table.
pokeauto_bridge = {
  handle      = handle,
  advance     = advance_input,
  set_client  = function(c) client = c end,
  mask        = function() return mask_now end,
  pending     = function() return #queue end,
}
