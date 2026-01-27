AI-Auto-User-Coder (AI_Coder_Controller) — Multi-Project Sidebar Extension
=========================================================================

This file extends the previous instructions with a new feature:
- Sidebar includes a "Projects" button
- Clicking "Projects" lists all user-created projects as buttons
- One mandatory project is "Self-Improve"
- Each project has its own objectives and instructions files
- "Self-Improve" project guides the program to use Copilot for self-analysis and improvement

----------------------------------------------------------------
Section 1 — Sidebar Multi-Project Feature
----------------------------------------------------------------
UI requirements:
- Add a "Projects" button to the sidebar
- When clicked, dynamically list project names (buttons) from a `projects` directory
- Each project button opens its own objectives/instructions files in the sidebar list
- Projects are stored under: C:\Users\yerbr\AI_Algorithms\AI_Coder_Controller\projects\

Directory structure:
- projects\
  - Self-Improve\
    - objectives.md
    - instructions.md
  - <OtherUserProjects>\
    - objectives.md
    - instructions.md

UI behavior:
- Sidebar shows "Projects" button
- Clicking "Projects" expands list of project names
- Clicking a project name loads its objectives/instructions into the sidebar file list

----------------------------------------------------------------
Section 2 — Self-Improve Project
----------------------------------------------------------------
Project name: Self-Improve

Files:

File: projects\Self-Improve\objectives.md
-----------------------------------------
# Objectives — Self-Improve Project
1. Analyze current modules and file system structure
2. Use Copilot to search for improvements in architecture, safety, and automation
3. Create a txt file listing modules, functions, and file system mapping
4. Upload this txt file to Copilot for analysis
5. Obtain Copilot’s suggestions for self-improvement
6. Apply improvements to codebase (update modules, add features)
7. Log changes and new capabilities

File: projects\Self-Improve\instructions.md
-------------------------------------------
# Instructions — Self-Improve Project
- Software to use:
  - VSCode for editing and running modules
  - Copilot Chat inside VSCode for improvement suggestions
- Procedure:
  1. Collect metadata: list all modules (capture.py, control.py, policy.py, ui.py, vsbridge.py, main.py)
  2. Generate a txt file with module names, functions, and file system layout
  3. Upload txt file to Copilot Chat
  4. Ask Copilot: “Analyze this architecture and suggest improvements for modularity, safety, and automation.”
  5. Scroll and read Copilot’s response
  6. Summarize improvements into a new file: projects\Self-Improve\improvements.md
  7. Apply improvements iteratively, updating objectives.md with new goals
- Safety:
  - Always keep a backup of original modules before applying changes
  - Log every improvement applied in logs\self_improve.log

File: projects\Self-Improve\improvements.md (auto-generated after Copilot analysis)
-------------------------------------------
# Improvements (example placeholder)
- Suggested refactor: split vsbridge.py into vscode.py and copilot.py
- Add error handling for pyautogui hotkeys
- Improve UI responsiveness with async event loop
- Add configuration for multiple Copilot queries per objective

----------------------------------------------------------------
Section 3 — Integration Notes
----------------------------------------------------------------
- The UI must scan `projects\` directory for subfolders
- Each subfolder name becomes a project button
- Clicking a project loads its objectives.md and instructions.md into the sidebar file list
- The “Self-Improve” project is mandatory and pre-created
- Logs of self-improvement actions are stored in logs\self_improve.log

----------------------------------------------------------------
Section 4 — Quick-start for Self-Improve
----------------------------------------------------------------
1. Create directory: C:\Users\yerbr\AI_Algorithms\AI_Coder_Controller\projects\Self-Improve\
2. Save objectives.md and instructions.md as above
3. Run program
4. In sidebar, click “Projects” → “Self-Improve”
5. Program parses objectives and executes:
   - Collects module/file info
   - Creates txt file listing modules and functions
   - Uploads to Copilot Chat
   - Reads Copilot’s suggestions
   - Logs improvements
6. Apply improvements iteratively

End of file.