-- Installed by the Knights Mod Editor's "Test Segment" command.
--
-- This module is loaded BEFORE every other module so that it can watch
-- the kts.LoadSegments calls the other modules make. ktseditor_segtest
-- (loaded last) then looks up the segments file under test here and
-- gets both the loaded segments and the exact tile table the mod passed
-- for that file - however the mod happened to name or scope that table.

mod.RegisterMod{ name = "ktseditor_segtest_hook", version = "1.0" }

-- Mirror of the game's VFS::normalizePath: empty and "." components are
-- dropped, ".." pops the previous component, and the result has no
-- leading slash (so "/base/maps/x.txt" and "base/./maps/x.txt" agree).
local function normalize(path)
    local parts = {}
    for comp in string.gmatch(path, "[^/\\]+") do
        if comp == ".." then
            table.remove(parts)
        elseif comp ~= "." then
            table.insert(parts, comp)
        end
    end
    return table.concat(parts, "/")
end

-- One entry per kts.LoadSegments call, in call order:
-- { path = <normalized VFS path>, root_path = <normalized VFS path>,
--   tile_table = ..., segments = ... }
local calls = {}

local real_load_segments = kts.LoadSegments

kts.LoadSegments = function(tile_table, filename)
    local segments = real_load_segments(tile_table, filename)
    -- The game resolves a relative filename against _CWD (the directory
    -- of the Lua file being executed) if the file exists there, and
    -- otherwise against the VFS root. Lua can't tell which applied, so
    -- both candidates are recorded (they coincide for an absolute
    -- filename) and ktseditor_segtest accepts either.
    local root_path = normalize(filename)
    local path = root_path
    if type(filename) == "string" and not string.match(filename, "^[/\\]") then
        path = normalize((_CWD or "") .. "/" .. filename)
    end
    calls[#calls + 1] = {
        path = path,
        root_path = root_path,
        tile_table = tile_table,
        segments = segments,
    }
    return segments
end

-- Exposed to ktseditor_segtest via require("ktseditor_segtest_hook").
segtest_normalize = normalize
segtest_calls = calls
