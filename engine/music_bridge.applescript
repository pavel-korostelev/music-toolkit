on safeRole(el)
    tell application "System Events"
        try
            return role of el
        end try
    end tell
    return ""
end safeRole

on mainWindow()
    tell application "System Events" to tell process "Music"
        repeat 30 times
            try
                return first UI element whose role is "AXWindow"
            end try
            tell application "Music" to activate
            delay 0.2
        end repeat
        error "Music window is unavailable"
    end tell
end mainWindow

on navigationItems(w)
    set nav to my findToolbar(w, 5)
    if nav is missing value then return {}
    tell application "System Events" to return entire contents of nav
end navigationItems

on findToolbar(rootEl, remainingDepth)
    set r to my safeRole(rootEl)
    if r is "AXToolbar" then return rootEl
    if remainingDepth is 0 or r is in {"AXTable", "AXOutline", "AXScrollArea"} then return missing value
    tell application "System Events"
        try
            set childItems to UI elements of rootEl
        on error
            return missing value
        end try
    end tell
    repeat with childEl in childItems
        set foundToolbar to my findToolbar(childEl, remainingDepth - 1)
        if foundToolbar is not missing value then return foundToolbar
    end repeat
    return missing value
end findToolbar

on albumTable(w)
    tell application "System Events" to tell process "Music"
        return table 1 of scroll area 2 of splitter group 1 of w
    end tell
end albumTable

on findMenu(rootEl, remainingDepth)
    set r to my safeRole(rootEl)
    if r is "AXMenu" then return rootEl
    -- Album tables can have hundreds of descendants. Their selected track's
    -- action menu is handled separately, so never traverse all their rows.
    if remainingDepth is 0 or r is "AXTable" then return missing value
    tell application "System Events"
        try
            set childItems to UI elements of rootEl
        on error
            return missing value
        end try
    end tell
    repeat with childEl in reverse of childItems
        set foundMenu to my findMenu(childEl, remainingDepth - 1)
        if foundMenu is not missing value then return foundMenu
    end repeat
    return missing value
end findMenu

on contextMenu(w)
    try
        set trackTable to my albumTable(w)
        tell application "System Events"
            set selectedRows to rows of trackTable whose selected is true
            if (count selectedRows) is 1 then
                set selectedRow to item 1 of selectedRows
                set parts to entire contents of selectedRow
                repeat with part in parts
                    if my safeRole(part) is "AXButton" then
                        try
                            if (value of attribute "AXIdentifier" of part) is "actionButton" then
                                set trackMenu to menu 1 of part
                                if exists trackMenu then return trackMenu
                            end if
                        end try
                    end if
                end repeat
            end if
        end tell
    end try
    return my findMenu(w, 10)
end contextMenu

on normalizedMenuName(menuName)
    set resultText to ""
    set pendingSpace to false
    set whitespaceChars to {space, tab, linefeed, return, character id 160, character id 8239}
    repeat with ch in characters of (menuName as text)
        set charText to ch as text
        if charText is in whitespaceChars then
            if resultText is not "" then set pendingSpace to true
        else
            if pendingSpace then set resultText to resultText & space
            set resultText to resultText & charText
            set pendingSpace to false
        end if
    end repeat
    return resultText
end normalizedMenuName

-- Shared by actual UI selection and the GUI-free resolve-menu diagnostics.
-- Exact normalized equality avoids clicking similarly named commands.
on menuChoice(menuNames, wantedNames)
    set foundIndex to 0
    repeat with n from 1 to count menuNames
        set actualName to my normalizedMenuName(item n of menuNames)
        if actualName is not "" then
            repeat with wantedName in wantedNames
                set expectedName to my normalizedMenuName(wantedName)
                ignoring case
                    set isMatch to actualName is expectedName
                end ignoring
                if isMatch then
                    if foundIndex is not 0 and foundIndex is not n then error "Ambiguous Music menu command; nothing clicked"
                    set foundIndex to n
                    exit repeat
                end if
            end repeat
        end if
    end repeat
    return foundIndex
end menuChoice

on run argv
    set taskName to item 1 of argv
    if taskName is "resolve-menu" then
        -- Pure diagnostic: no app activation, UI lookup, or clicks.
        set wantedCount to (item 2 of argv) as integer
        if wantedCount < 1 or (count argv) < (2 + wantedCount) then error "Invalid resolve-menu arguments"
        set wantedNames to items 3 thru (2 + wantedCount) of argv
        set menuNames to {}
        if (count argv) > (2 + wantedCount) then set menuNames to items (3 + wantedCount) thru -1 of argv
        return my menuChoice(menuNames, wantedNames)
    end if
    if taskName is "search" then tell application "Music" to activate
    set w to my mainWindow()
    if taskName is "search" then
        set queryText to item 2 of argv
        tell application "System Events" to tell process "Music" to key code 53
        set w to my mainWindow()
        tell application "System Events" to tell process "Music"
            set itemsList to my navigationItems(w)
            repeat with el in itemsList
                if my safeRole(el) is "AXButton" then
                    try
                        if (value of attribute "AXIdentifier" of el) is "backButton" then
                            click el
                            delay 1
                            exit repeat
                        end if
                    end try
                end if
            end repeat
        end tell
        set w to my mainWindow()
        tell application "System Events" to tell process "Music"
            set selected of row 1 of outline 1 of scroll area 1 of splitter group 1 of w to true
        end tell
        delay 2
        repeat 20 times
            try
                set w to my mainWindow()
                tell application "System Events" to tell process "Music"
                    set itemsList to my navigationItems(w)
                    repeat with el in itemsList
                        if my safeRole(el) is "AXTextField" then
                            set ident to ""
                            try
                                set ident to value of attribute "AXIdentifier" of el as text
                            end try
                            try
                                if ident is not "filterField" then
                                    set value of el to queryText
                                    set focused of el to true
                                    key code 36
                                    return "search submitted"
                                end if
                            end try
                        end if
                    end repeat
                end tell
            end try
            delay 0.5
        end repeat
        error "Catalog search field was not found"
    else if taskName is "candidates" then
        set outText to ""
        tell application "System Events" to tell process "Music"
            set itemsList to entire contents of w
            repeat with el in itemsList
                if my safeRole(el) is "AXMenuButton" then
                    try
                        set ident to value of attribute "AXIdentifier" of el as text
                        if ident contains "Music.shelfItem.TrackLockup[id=" then set outText to outText & ident & tab & (name of el as text) & linefeed
                    end try
                end if
            end repeat
        end tell
        return outText
    else if taskName is "menu" then
        set catalogID to item 2 of argv
        tell application "System Events" to tell process "Music"
            set itemsList to entire contents of w
            repeat with el in itemsList
                try
                    set ident to value of attribute "AXIdentifier" of el as text
                    if ident contains "TrackLockup[id=" & catalogID & "," then
                        set availableActions to name of actions of el
                        if "AXShowMenu" is in availableActions then
                            perform action "AXShowMenu" of el
                            return "menu shown"
                        end if
                        return availableActions
                    end if
                end try
            end repeat
        end tell
        error "Track menu was not found"
    else if taskName is "menu-command" then
        if (count argv) < 2 then error "No menu captions supplied"
        set wantedNames to items 2 thru -1 of argv
        set activeMenu to my contextMenu(w)
        if activeMenu is missing value then error "Context menu is not available"
        set menuNames to {}
        set menuEntries to {}
        tell application "System Events" to tell process "Music"
            set itemsList to menu items of activeMenu
            repeat with el in itemsList
                if my safeRole(el) is "AXMenuItem" then
                    try
                        if enabled of el then
                            set end of menuNames to (name of el as text)
                            set end of menuEntries to contents of el
                        end if
                    end try
                end if
            end repeat
        end tell
        set chosenIndex to my menuChoice(menuNames, wantedNames)
        if chosenIndex is 0 then error "Menu command was not found (supported captions: Russian/English); nothing clicked"
        set chosenEntry to item chosenIndex of menuEntries
        tell application "System Events" to tell process "Music" to click chosenEntry
        return "menu command clicked"
    else if taskName is "album-data" or taskName is "album-menu" then
        tell application "System Events" to tell process "Music"
            set itemsList to {my albumTable(w)}
            repeat with el in itemsList
                if my safeRole(el) is "AXTable" then
                    set selectedRows to rows of el whose selected is true
                    if (count selectedRows) is not 1 then error "Expected one selected album track"
                    set trackRow to item 1 of selectedRows
                    set rowParts to entire contents of trackRow
                    if taskName is "album-data" then
                        set outText to ""
                        repeat with part in rowParts
                            if my safeRole(part) is "AXStaticText" then set outText to outText & (value of part as text) & tab
                        end repeat
                        return outText
                    else
                        repeat with part in rowParts
                            if my safeRole(part) is "AXButton" then
                                try
                                    if (value of attribute "AXIdentifier" of part) is "actionButton" then
                                        click part
                                        return "album menu clicked"
                                    end if
                                end try
                            end if
                        end repeat
                    end if
                end if
            end repeat
        end tell
        error "Selected album track was not found"
    else if taskName is "snapshot" then
        set outText to ""
        tell application "System Events" to tell process "Music"
            set itemsList to entire contents of w
            repeat with el in itemsList
                try
                    set r to my safeRole(el)
                    if r is in {"AXButton", "AXMenuButton", "AXMenuItem", "AXTextField", "AXStaticText", "AXRadioButton"} then
                        set d to ""
                        set v to ""
                        set ident to ""
                        set n to ""
                        try
                            set n to name of el as text
                        end try
                        try
                            set d to description of el as text
                        end try
                        try
                            set v to value of el as text
                        end try
                        try
                            set ident to value of attribute "AXIdentifier" of el as text
                        end try
                        set outText to outText & r & tab & d & tab & v & tab & ident & tab & n & linefeed
                    end if
                end try
            end repeat
        end tell
        return outText
    end if
end run
