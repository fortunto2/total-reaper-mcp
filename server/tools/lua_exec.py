"""
Run Lua inside REAPER, and the note-level MIDI tools built on it.

The bridge calls one ReaScript function per request with JSON arguments, so a
MediaTrack or MediaItem_Take can never be passed back in: anything that walks
tracks -> items -> takes -> notes has to happen inside REAPER. `run_lua` does
that: the code is written to a script, registered as an action, run, and its
`return` value comes back as JSON. One request, any depth of API.
"""

import asyncio
import json
import uuid
from typing import Optional

from ..bridge import bridge

# Minimal JSON encoder for the value the user's code returns.
_LUA_PRELUDE = r'''
local function __enc(v)
  local t = type(v)
  if v == nil then return "null"
  elseif t == "boolean" then return v and "true" or "false"
  elseif t == "number" then
    if v ~= v or v == math.huge or v == -math.huge then return "null" end
    if math.type and math.type(v) == "integer" then return tostring(v) end
    return string.format("%.17g", v)
  elseif t == "string" then
    return '"' .. v:gsub('[%c"\\]', function(c)
      local m = {['"'] = '\\"', ['\\'] = '\\\\', ['\n'] = '\\n', ['\r'] = '\\r', ['\t'] = '\\t'}
      return m[c] or string.format("\\u%04x", c:byte())
    end) .. '"'
  elseif t == "table" then
    local n = #v
    if n > 0 or next(v) == nil then
      local out = {}
      for i = 1, n do out[i] = __enc(v[i]) end
      return "[" .. table.concat(out, ",") .. "]"
    end
    local out = {}
    for k, x in pairs(v) do out[#out + 1] = __enc(tostring(k)) .. ":" .. __enc(x) end
    return "{" .. table.concat(out, ",") .. "}"
  end
  return __enc(tostring(v))
end
'''


async def run_lua(code: str, timeout: float = 15.0) -> str:
    """Run Lua code inside REAPER and return its `return` value as JSON.

    The code is a function body: use the `reaper.*` API freely (handles stay
    inside REAPER) and `return` a value — table, number, string, boolean.
    Wrap edits in reaper.Undo_BeginBlock2(0) / reaper.Undo_EndBlock2(0, "what", -1)
    so they undo as one step. Errors come back as {"ok": false, "error": ...}.
    """
    run_id = uuid.uuid4().hex[:12]
    script = bridge.bridge_dir / f"run_{run_id}.lua"
    result = bridge.bridge_dir / f"run_{run_id}.json"
    script.write_text(
        _LUA_PRELUDE
        + f"local __ok, __res = pcall(function()\n{code}\nend)\n"
        + f'local __f = io.open({json.dumps(str(result))}, "w")\n'
        + '__f:write(__ok and (\'{"ok":true,"result":\' .. __enc(__res) .. "}") '
        + 'or (\'{"ok":false,"error":\' .. __enc(tostring(__res)) .. "}"))\n'
        + "__f:close()\n",
        encoding="utf-8",
    )
    try:
        reg = await bridge.call_lua("AddRemoveReaScript", [True, 0, str(script), True])
        cmd = reg.get("ret")
        if not reg.get("ok") or not cmd:
            raise Exception(f"could not register the script: {reg}")
        try:
            await bridge.call_lua("Main_OnCommand", [cmd, 0])
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while not result.exists():
                if loop.time() > deadline:
                    raise Exception(f"no result after {timeout}s (script still running, or REAPER busy)")
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.02)  # let the write finish
            return result.read_text(encoding="utf-8")
        finally:
            await bridge.call_lua("AddRemoveReaScript", [False, 0, str(script), True])
    finally:
        script.unlink(missing_ok=True)
        result.unlink(missing_ok=True)


async def _lua_json(code: str) -> dict:
    out = json.loads(await run_lua(code))
    if not out.get("ok"):
        raise Exception(out.get("error"))
    return out["result"]


# Shared by the note tools: every MIDI take on a track, with its notes.
_TAKES = r'''
local tr = reaper.GetTrack(0, TRACK)
if not tr then error("no track " .. TRACK) end
local function takes()
  local out = {}
  for i = 0, reaper.CountTrackMediaItems(tr) - 1 do
    local item = reaper.GetTrackMediaItem(tr, i)
    local take = reaper.GetActiveTake(item)
    if take and reaper.TakeIsMIDI(take) then
      out[#out + 1] = {item = item, take = take, index = i}
    end
  end
  return out
end
local function notes(take)
  local _, nn = reaper.MIDI_CountEvts(take)
  local out = {}
  for k = 0, nn - 1 do
    local _, sel, mut, s, e, ch, p, v = reaper.MIDI_GetNote(take, k)
    out[#out + 1] = {k = k, sel = sel, mut = mut, s = s, e = e, ch = ch, p = p, v = v}
  end
  return out
end
local NAMES = {"C","C#","D","D#","E","F","F#","G","G#","A","A#","B"}
local function name(p) return NAMES[p % 12 + 1] .. tostring(p // 12 - 1) end
'''


async def midi_note_stats(track_index: int) -> str:
    """Note range and pitch histogram of a track's MIDI (all items), so register
    clashes can be read before mixing: e.g. a chord part whose lowest notes sit
    on the bass's 2nd harmonic. Pooled copies count once per item, as heard."""
    res = await _lua_json(_TAKES.replace("TRACK", str(track_index)) + r'''
local hist, total, lo, hi, items = {}, 0, 128, -1, 0
for _, t in ipairs(takes()) do
  items = items + 1
  for _, n in ipairs(notes(t.take)) do
    hist[n.p] = (hist[n.p] or 0) + 1
    total = total + 1
    if n.p < lo then lo = n.p end
    if n.p > hi then hi = n.p end
  end
end
local _, tname = reaper.GetTrackName(tr)
local h = {}
for p = 0, 127 do if hist[p] then h[#h + 1] = {pitch = p, name = name(p), count = hist[p], hz = 440 * 2 ^ ((p - 69) / 12)} end end
return {track = tname, items = items, notes = total, lowest = total > 0 and name(lo) or nil, highest = total > 0 and name(hi) or nil, histogram = h}
''')
    return json.dumps(res, ensure_ascii=False)


async def midi_transpose_notes(track_index: int, semitones: int,
                               below_pitch: Optional[int] = None,
                               above_pitch: Optional[int] = None,
                               drop_doubled: bool = True,
                               dry_run: bool = False) -> str:
    """Move a track's notes in a pitch range by `semitones` — the arrangement
    fix for masking (e.g. below_pitch=48, semitones=12: a chord part's notes
    under C3 up an octave, off the bass). Only notes with pitch < below_pitch
    and/or > above_pitch move; without either, all notes move. drop_doubled:
    a note whose target pitch already sounds at the same time is deleted
    instead of doubled. Pooled items are edited once (a dry run cannot tell
    pooled copies apart yet, so it counts every item). One undo step."""
    if below_pitch is None and above_pitch is None and semitones == 0:
        return "nothing to do"
    lua_nil = lambda v: "nil" if v is None else str(int(v))
    res = await _lua_json(_TAKES.replace("TRACK", str(track_index)) + f'''
local BELOW, ABOVE, SEMI = {lua_nil(below_pitch)}, {lua_nil(above_pitch)}, {int(semitones)}
local DROP, DRY = {str(drop_doubled).lower()}, {str(dry_run).lower()}
''' + r'''
local function hit(p)
  if BELOW == nil and ABOVE == nil then return true end
  return (BELOW ~= nil and p < BELOW) or (ABOVE ~= nil and p > ABOVE)
end
-- Read everything first: a pooled copy changes when its source is edited,
-- so a take whose MIDI hash moved since the read is skipped, not edited twice.
local plan = {}
for _, t in ipairs(takes()) do
  local _, hash = reaper.MIDI_GetHash(t.take, false, "")
  plan[#plan + 1] = {t = t, hash = hash, notes = notes(t.take)}
end
local moved, dropped, skipped, edited = 0, 0, 0, 0
if not DRY then reaper.Undo_BeginBlock2(0) end
for _, e in ipairs(plan) do
  local _, now = reaper.MIDI_GetHash(e.t.take, false, "")
  if now ~= e.hash then
    skipped = skipped + 1
  else
    local del, touched = {}, false
    for _, n in ipairs(e.notes) do
      if hit(n.p) then
        local target, dup = n.p + SEMI, false
        if DROP then
          for _, m in ipairs(e.notes) do
            -- only a note that stays put can be doubled; one that moves too leaves
            if m.p == target and not hit(m.p) and m.s < n.e and m.e > n.s then dup = true end
          end
        end
        if target < 0 or target > 127 or dup then
          dropped = dropped + 1; del[#del + 1] = n.k
        else
          moved = moved + 1
          if not DRY then reaper.MIDI_SetNote(e.t.take, n.k, n.sel, n.mut, n.s, n.e, n.ch, target, n.v, true) end
        end
        touched = true
      end
    end
    if touched then edited = edited + 1 end
    if not DRY then
      table.sort(del, function(a, b) return a > b end)
      for _, k in ipairs(del) do reaper.MIDI_DeleteNote(e.t.take, k) end
      reaper.MIDI_Sort(e.t.take)
    end
  end
end
local _, tname = reaper.GetTrackName(tr)
if not DRY then
  reaper.Undo_EndBlock2(0, string.format("Transpose %s notes %+d st (%d moved, %d dropped)", tname, SEMI, moved, dropped), -1)
  reaper.UpdateArrange()
end
return {track = tname, dry_run = DRY, moved = moved, dropped = dropped, takes_edited = edited, pooled_copies_skipped = skipped}
''')
    return json.dumps(res, ensure_ascii=False)


def register_lua_exec_tools(mcp) -> int:
    """Register run_lua and the note tools."""
    tools = [
        (run_lua, "Run Lua inside REAPER (full reaper.* API, handles stay inside) and get its return value as JSON"),
        (midi_note_stats, "Note range and pitch histogram of a track's MIDI — read registers before mixing"),
        (midi_transpose_notes, "Move a track's notes in a pitch range by N semitones (e.g. notes under C3 up an octave), one undo step"),
    ]
    for func, _desc in tools:
        mcp.tool()(func)
    return len(tools)
