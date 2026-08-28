-- Installed by the Knights Mod Editor's "Test Segment" command.
--
-- Loaded AFTER every other module. Sets up a game consisting of just the
-- one segment being edited, so it can be playtested in the real game.
-- Relies on ktseditor_segtest_hook (loaded first) having recorded the
-- kts.LoadSegments calls made by the other modules.
--
-- The "@@...@@" markers below are filled in by the editor when it copies
-- this template to a temporary modules directory.

mod.RegisterMod{ name = "ktseditor_segtest", version = "1.0" }

-- The module that owns the segments file, and the file's name relative
-- to that module's directory.
local module_name       = "@@MODULE_NAME@@"
local segments_filename = "@@SEGMENTS_FILENAME@@"

-- Which segment in the file to test (1-based, in file order).
local segment_index     = tonumber("@@SEGMENT_INDEX@@")

-- tile_table entry used to fill the ring of wall around the segment.
local wall_tile_index   = tonumber("@@WALL_TILE_INDEX@@")

-- Fallback for a file that no module passes to kts.LoadSegments (e.g. a
-- new file not yet referenced from Lua): the tile table is taken from
-- this module-level global instead, as the editor itself does.
local fallback_module   = "@@TILE_TABLE_MODULE@@"
local fallback_table    = "@@TILE_TABLE_NAME@@"

local hook = require("ktseditor_segtest_hook")

local prefix = mod.GetModPrefix(module_name)
if prefix == nil then
    error("ktseditor_segtest: module '" .. module_name .. "' is not loaded")
end
local target = hook.segtest_normalize(prefix .. segments_filename)

-- Find the (last) kts.LoadSegments call that loaded the file under test:
-- by the _CWD-relative path first, then by the VFS-root fallback path
-- (see the hook module).
local function find_call(field)
    local tt, segs
    for _, call in ipairs(hook.segtest_calls) do
        if call[field] == target then
            tt, segs = call.tile_table, call.segments
        end
    end
    return tt, segs
end
local tile_table, segments = find_call("path")
if tile_table == nil then
    tile_table, segments = find_call("root_path")
end

if tile_table == nil then
    if fallback_module == "" then
        error("ktseditor_segtest: " .. segments_filename .. " is not loaded by "
              .. "any kts.LoadSegments call, and no fallback tile table is known")
    end
    tile_table = rawget(require(fallback_module), fallback_table)
    if type(tile_table) ~= "table" then
        error("ktseditor_segtest: " .. segments_filename .. " is not loaded by "
              .. "any kts.LoadSegments call, and no table named '" .. fallback_table
              .. "' was found in module '" .. fallback_module .. "'")
    end
    segments = kts.LoadSegments(tile_table, prefix .. segments_filename)
end

local segment = segments[segment_index]
if segment == nil then
    error("ktseditor_segtest: " .. segments_filename .. " has no segment number "
          .. tostring(segment_index) .. " (it contains " .. #segments .. ")")
end

local wall = tile_table[wall_tile_index]
if wall == nil then
    error("ktseditor_segtest: tile table has no entry " .. tostring(wall_tile_index)
          .. " to use as the wall tile")
end

local function start_game()
    -- Lay out a dungeon consisting of just this one segment, not rotated.
    kts.LayoutDungeon {
        layout = {
            width = 1,
            height = 1,
            data = { { type = "block" } }
        },
        wall = wall,
        segments = { segment },
        entry_type = "none",
        allow_rotate = false
    }

    -- Spawn at a random position (don't rely on knight homes).
    kts.SetRespawnType("anywhere")
end

-- Replace the menu so that autostart (-a) goes straight into this game.
kts.MENU = {
    start_game_func = start_game,
    items = { }
}
