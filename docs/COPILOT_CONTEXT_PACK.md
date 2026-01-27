# Copilot Context Pack Workflow

This project can generate a compact **Context Pack** that summarizes the AI_Coder_Controller workspace for Copilot (VS Code Chat or the Windows Copilot app).

## Artifact

- Main file: `Copilot_Attachments/ContextPack_Current.md`
- Produced by:
  - `Scripts/prepare_context_pack.py`
  - The VS Code task **"Test/Gather/Assess Workflow"** (which runs the script near the end)
  - The Agent Mode objective: `Prepare context pack for Copilot` (handled via Policy -> TerminalAgent)

The context pack links and/or excerpts:
- Project overview: `README.md`
- Objectives: `config/objectives*.md`
- Key policy/config sections: `config/policy_rules.json`, `config/vscode_orchestrator.json`
- Structure inventories: `Copilot_Attachments/*_fs_structure.txt`
- Agent Mode inventory: `Copilot_Attachments/AgentModeModules.txt`
- Latest Test/Gather/Assess summary: `logs/tests/workflow_summary_*.json`
- Latest OCR assessment report (if present): `Archive_OCR_Images_Assessments/OBSERVED_OCR_IMAGES_*.md`

## Recommended Usage – VS Code Chat

1. Run the **"Test/Gather/Assess Workflow"** task (or execute `Scripts/workflow_test_gather_assess.py`) to refresh the context pack.
2. Open `Copilot_Attachments/ContextPack_Current.md` in VS Code.
3. In VS Code Chat, paste either the whole file or the top sections (overview, objectives, key files).
4. Then send a prompt such as:

> You are GPT-5.1. You have just received a context pack for the AI_Coder_Controller project in this conversation (ContextPack_Current.md). Use it as your primary reference. First, summarize the current objectives and architecture in your own words. Then propose the next 3–5 safe, concrete edits or experiments to improve the Agent Mode workflow.

5. For follow-up questions, you can send shorter prompts like:

> Using the same context pack from earlier in this conversation, focus on the VS Code multi-window orchestrator components and suggest improvements to error handling and logging.

## Recommended Usage – Windows Copilot App

The Windows Copilot app cannot directly read local files, so provide the context via text:

1. Refresh the context pack as above.
2. Open `Copilot_Attachments/ContextPack_Current.md` and copy:
   - The header, project overview, objectives, and key entry points sections.
3. In the Copilot app window, paste that text as your first message.
4. Immediately follow with an instruction, for example:

> You are GPT-5.1. I just pasted a context pack for the AI_Coder_Controller project (it includes README excerpts, objectives, policies, inventories, and recent workflow summaries). Read it carefully and base your answers on it. Next, help me refine the Agent Mode objectives and recovery logic to be safer and more deterministic.

5. When you later paste logs or OCR reports, remind Copilot to interpret them in light of the same context pack.

## Objectives & Policy Integration

- In `config/objectives.md`, the line:
  - `Prepare context pack for Copilot (generates Copilot_Attachments/ContextPack_Current.md)`
  triggers the Policy mapping in `src/policy.py` to run `Scripts.prepare_context_pack` via the Agent Terminal.
- This allows **Agent Mode** runs to regenerate the context pack as part of their normal objective list before asking Copilot questions.

## Orchestrator Integration (VS Code Chat)

- `config/vscode_orchestrator.json` defines message templates used by the VS Code multi-window orchestrator.
- When you are running workflows where a context pack has been pasted into chat, prefer templates that:
  - Ask for objective state.
  - Remind Copilot to base its reasoning on the previously shared context (e.g., README, ContextPack_Current).

A simple example template text:

> Please summarize the current objectives and task state using the project context you have already seen (README and/or ContextPack_Current.md). Indicate whether any tasks remain, and if none are left, propose the next safe actions.
