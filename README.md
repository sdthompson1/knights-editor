# Knights Mod Editor

A tool for creating and editing mods for
[Knights](https://www.knightsgame.org.uk/), the multiplayer dungeon game.


## What this does

The Knights Mod Editor allows you to:
 - Open a Knights mod
 - View and edit the mod's Lua scripts
 - Edit dungeon maps ("segments"):
    - Create new dungeon rooms, or edit existing ones
    - Set up switch actions (switches can open/close doors, shoot
      bolts, or run custom Lua functions)
    - Launch the game in a test mode, to test out the map you are editing
 - Browse Lua state, via a filterable tree of Lua globals
    - This includes a Properties panel, which allows you to preview
      the mod's graphics, animations and sounds, as they will appear in
      the game

You don't *have* to use this tool to create Knights mods -- it's
perfectly possible to create mods using other tools (such as VS Code
for Lua script editing, MS Paint for graphics, etc.) -- but this tool
does have some unique capabilities that you won't find elsewhere (such
as editing dungeon maps), so it might be useful for those purposes.


## Requirements

- Python 3.10 or later
- A copy of Knights (at least the `knights_data` directory is needed)


## How to Run

First, run `pip install -r requirements.txt` to install required
dependencies (PySide6 and lupa).

Then, run `python -m knights_editor` to open the editor.

On first run, the editor will ask you to set your `knights_data`
directory, and also (optionally) the location of the Knights
executable (the latter is needed for testing edited map segments in
the game). These can be changed later by going to File -> Settings, if
necessary.


## Opening a mod

Use File -> Open Mod to open a mod. You will need to navigate to the
directory of your mod (i.e. the folder containing `init.lua`), then
click Open.

For example, you could try opening the "base" mod (navigate to
"knights_data/modules/base" in your Knights installation -- found
under "steamapps/common/Knights" if you are using Steam), which will
enable you to view all of the Lua scripts, graphics, dungeon rooms
etc. that are part of the "base" module that comes with the game.

If you get an error saying something like "require: module 'foo' is
not registered ... Tile graphics may be missing or incomplete", this
means the mod editor was not able to find all the Lua dependencies of
your mod. See "Mod Dependencies" below for more information about
this.


## Editor main screen

The editor main screen is split into a tree view on the left, and a
file editing area (initially empty) on the right, e.g.:

![Knights Editor screenshot, showing a file list on the left, and a
 blank area on the right](screenshots/screen1.png)

The tree view on the left is split into two tabs, "Files" showing all
files in the mod's directory (and subdirectories), and "Lua" which we
will cover below.

By double clicking a file in the tree view, the corresponding file
will open in the editor panel on the right. For example, here we are
editing a Lua file:

![Knights Editor screenshot, showing a file list on the left, and the
 contents of init.lua on the right](screenshots/screen2.png)

After double clicking on one of the map files, we can edit the actual
dungeon room layouts (this will be described in more detail below):

![Knights Editor screenshot, showing one of the gnome book dungeon
 layouts](screenshots/screen3.png)

Other file types (graphics, sounds etc.) can be viewed in the file
tree, but cannot currently be opened in the editor.

As mentioned above, there is also a "Lua" tab (near the bottom left of
the screen). Clicking this will open a view of every Lua object
(table, function etc) defined by the mod (and its dependencies). This
is a tree view, so you can open tables to inspect their contents.
Clicking on any particular object shows a properties panel. For
example, we can inspect the properties of `t_barrel` (the "barrel"
tile) as follows:

![Knights Editor screenshot, showing Lua tree view open at the
 "barrel" tile on the left, and the text of "init.lua" on the
 right](screenshots/screen4.png)

The Lua properties panel can be particularly useful for checking how
animations, graphics, sounds etc. will appear in the game. Try
clicking on different types of object in the tree view, and seeing
what properties are displayed for each.


## The dungeon map editor

Knights dungeons are built out of segments (see [kts.LayoutDungeon
documentation](https://www.knightsgame.org.uk/lua/LayoutDungeon) for a
full description). A segment is basically a pre-made 12x12 area of the
dungeon, divided up into individual rooms. The Knights dungeon
generator works by laying out one or more of these segments next to
each other, to form a dungeon layout.

A "segments file" contains multiple segments, each with its own name.
The name is not shown anywhere in-game and is only used within the
editor. (The segments that come with the game have simple alphanumeric
names like "A01.2" or "Z03.3".) The list of segments can be seen to
the left-hand side of the dungeon view area. Double click a segment to
open it; drag a segment to change its position in the list;
right-click to add, delete or duplicate an existing segment.

The right-hand side of the screen also has a "Segment Properties"
area. You can rename a segment by editing the Name, or resize it by
editing Width or Height. (Non-12x12 segments will not work with the
standard Knights dungeon generator, but they can be useful for special
purposes, e.g. the Tutorial map is one large segment.)


### Editing modes

Once you have opened a segment, four editing modes are available:
Select, Paint, Switches and Rooms (selectable by the radio buttons
below the main map view).

"Select" allows dragging out a rectangular area on the main dungeon
view, which you can then Cut, Copy and Paste (useful for moving a room
to a new position on the map, or creating multiple copies of rooms),
or Clear (useful if you made a mistake and just want to wipe out a
large area).

"Paint" is the standard painting mode. Select a tile by left-clicking
it in the tile palette (at the bottom), then left-click on the map to
place that tile. You can also use other mouse buttons (e.g. right or
middle button) to select and paint with multiple tiles at the same
time (e.g. you might set up a "wall" tile on the left mouse button,
and a "floor" on the right button). Also, control-clicking a tile on
the map will select that tile for painting.

"Switches" allows switch effects to be set up. On first entering
switch mode, the existing switches will be highlighted in red:

![Knights Editor screenshot, showing one of the gnome book rooms, with
 switches and pressure plates highlighted in
 red](screenshots/screen5.png)

A switch can either be placed on a lever (in which case the switch
takes effect when the lever is pulled), or on a floor square (in which
case the switch takes effect when a creature walks onto that square).
(More specifically, the effect is added as an `on_activate` or
`on_walk_over` effect, depending on whether the square is
"approachable" or "walkable".)

Clicking on a tile selects it, which displays the defined effect(s) of
the switch in the editor:

![Knights Editor screenshot, showing one of the gnome book rooms. A
 lever is selected; a line marked "toggle" shows that the lever
 toggles open or closed one of the gates leading to the
 book.](screenshots/screen6.png)

In this example, the switch is defined to "toggle" one of the gnome
book's gates open or closed. In general, a switch can "toggle", "open"
or "close" a door or gate, or "shoot" a bolt in one of four
directions, or call a custom Lua function. A switch can have more than
one effect at the same time. To add a new effect, you can click one of
the buttons marked "Toggle", "Shoot N", etc., and then click the
target map position. To remove an effect, right-click the target
position. To add a new switch entirely, double click a map square, and
to delete a switch, right-click the switch's own square. (All of these
actions can be undone, using Edit -> Undo, if you mess something up!)

The custom Lua functions are not fully documented here, but they
involve calling a global Lua function with a name starting with "rf_"
(which stands for "room function"). See the Tutorial, e.g. the
"Chamber of Vampire Bats" switch, for examples of how to set this up.

The final editing mode is "Rooms", which looks like this:

![Knights Editor screenshot, showing one of the standard dungeon
 segments, being edited in "Rooms" mode. Each individual dungeon room
 has a subtle coloured highlight around it.](screenshots/screen7.png)

This is where you configure how your dungeon segment is split up into
individual rooms. To define a room, simply drag out its boundaries, or
right-click to delete an existing room. The rooms must be defined to
include their borders (exactly as they would appear in the game), and
rooms must overlap along their edges (so that an "edge" square will be
in exactly two rooms; "corners" can be in up to four rooms).

There are various rules about valid placement of rules. The "Room
Check" panel on the right-hand side indicates whether any violations
of these rules are found; clicking "Show on map" will show the
problems as red squares or crosses on the map. Mouse-over any problem
to find out what the issue is. Placing rooms may require some
trial-and-error until all problems are resolved.

There is also a "Delete All Rooms" button to allow you to start over,
and an "Auto Rooms" button which will try to place rooms
automatically. The "Auto Rooms" usually doesn't get the layout exactly
right, but it is useful as a starting point, which you can then edit
by hand. Note that "Auto Rooms" fills in only the areas not already
covered by a room, so you can use it in two ways: either start from a
blank slate and let Auto Rooms fill in everything; or start by
manually placing one or two rooms of your own design, and let Auto
Rooms fill in the rest.


### Testing a segment

Once you have finished editing a segment, go to Segment -> Test
Segment (or press Ctrl-T) to try it out in the game. This will launch
Knights with special command line options which will generate a
one-segment dungeon (consisting of only the segment currently being
edited) and spawn your knight at a random location within it. This
allows you to test that the room layouts have been set up correctly,
and that the switches are all working as you intended, for example.

For "Test Segment" to work, the path to the Knights executable must
have been set correctly in File -> Settings.


### Creating new segments files

To make an entirely new segments file, you can right-click the file
tree and select "New Segments File". This will make a new file,
initially containing one "blank" segment called "new_1".

To actually wire up the new segment into the game, you'll need to add
a `kts.LoadSegments` Lua call, and then do something with the Lua
segments table that results. The editor will suggest a code snippet
for you to copy and paste, but that is only a starting point. It is
advised to look into what the existing mods do with segments files,
for inspiration (e.g. search for "gnome_rooms" in the base module, to
see how the gnome book segments are added to the dungeon when a Tome
of Gnomes quest is selected).


## Mod Dependencies

Upon opening a mod, the editor will run the mod's Lua scripts (within
a sandboxed Lua environment). If the mod depends on other mods (via
`require`, `mod.GetRegisteredMod`, or similar), then the editor needs
to be able to find and run all of those dependencies as well.

By default, the search path for dependencies includes:
 - the "knights_data/modules" folder (this allows mods to use the 
   "base" module from the original game, without any additional
   setup);
 - the parent directory of the mod being loaded (e.g. if you are
   loading a mod from "C:\my_mods\more_zombies", then "C:\my_mods" is
   automatically in the search path);
 - any "Additional modules directories" added in File -> Settings.

The automatic dependency search works by looking for `init.lua` files
in all subfolders of the search path(s), and then scanning their text
for `mod.RegisterMod` calls. For example, if "C:\knights_mods" is an
"Additional modules directory", and "C:\knights_mods\toolbox\init.lua"
exists, and it contains `mod.RegisterMod{ name = "toolbox", version =
"1.0" }`, then the editor would resolve any `require("toolbox")` call
to the mod found in "C:\knights_mods\toolbox\". (Note that the name
that appears in the `require` and `mod.RegisterMod` calls doesn't
necessarily need to be the same as the folder name.)

The scan of `init.lua` uses a text regex search, rather than running
the `init.lua` code directly, but as long as the `mod.RegisterMod`
calls are written in a standard way (e.g., the code isn't computing
the mod name by concatenating strings, or something like that), then
the search should work fine and the mod should be recognised.

Note also that the game and editor will only recognise mod folders
whose names include only alphanumeric characters (a-z, A-Z, 0-9), dots
("."), hyphens ("-") or underscores ("_").

Sometimes the automatic dependency search doesn't work as expected, or
puts the mods into the wrong load order. In these cases, you can fix
the problem as follows:
 - Go to File -> Module Load Order
 - Turn off the checkbox marked "Infer load order automatically"
 - The list box shows all modules that were found by the above
   scanning process; you can enable and disable them, and move them up
   and down in the load order, just like in the "Mods" dialog in the
   game itself.

Note: You might want to add the Knights Steam Workshop content folder 
("steamapps/workshop/content/4146180" under your Steam installation)
to "Additional modules directories" (on File -> Settings) -- this will
allow you to edit mods that have dependencies on things that you have
downloaded from Steam Workshop.


## Creating a new mod

Currently, the tool does not allow you to create a new mod completely
from scratch. You will need to create a mod directory, and write at
least a minimal "init.lua" yourself, before you will be able to open
the mod in the editor tool.

While working on your own mods, you might want to put them in
"knights_data/modules" so that the game can find them easily (without
special command line options being needed). This will also allow you
to upload to Steam Workshop (see next section).


## Uploading to Steam Workshop

If you want to upload a mod of your own to Steam Workshop, this can
only be done in the game (not from the editor tool) currently.

To upload a mod:
 - Place the mod in the "knights_data/modules" directory, in a folder
   of its own, alongside "base" and "tutorial".
 - Create a preview image for Steam Workshop and save it as
   "preview.png", "preview.jpg", "preview.jpeg" or "preview.gif". This
   file must be 1 MB or less in size.
 - Start Knights and go to "Mods" from the main menu, then click
   "Upload Mod".
 - Select your mod from the list and click "Upload". (If it doesn't
   appear, check that you put it in the right folder, and that your
   mod's folder name only contains letters a-z or A-Z, numbers 0-9,
   hyphens, underscores or dots.)
 - The first time you upload a new mod, a new Workshop item will be
   created (set to "private" visibility initially), and a file
   "workshop_item_id.txt" will be created in your mod's directory,
   containing the Workshop item ID number. (This file, and the preview
   image, will not form part of the upload.) Once the upload is
   complete, you can go to Steam Workshop itself to edit the mod title
   and other properties, and set the item to public visibility when
   you are ready.
 - On the second or subsequent uploads, the game will read the item ID
   number from "workshop_item_id.txt", and will update the existing
   Workshop item with that ID number. This is how you can make updates
   to your existing mods.


## Contact

The author can be contacted by email: stephen@solarflare.org.uk


## About Knights

Knights is a multiplayer game in which players must explore randomly
generated dungeons and race to complete various quests. For more
information please visit the website at:
https://www.knightsgame.org.uk/


## Copyright/License Info

This program is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation; either version 2 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License along
with this program; if not, write to the Free Software Foundation, Inc.,
51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.


## AI Usage

The code was written with the help of the Claude Opus 5 and Fable 5
models. All code was reviewed manually by the author before release.
