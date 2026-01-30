# AI Coder Controller – Self-Improve Agent

## Goal
Continuously assess, test, and improve the AI_Coder_Controller project using the orchestrator demo, CLI pipelines, and test suite.

## Triggers
- On push to main
- On pull requests
- On a weekly schedule
- Manual run via workflow_dispatch

## Tasks
1. **Run orchestrator demos**
   - `python examples/run_demo.py --max-iterations 1`
   - `python -m src.orchestrator.cli --config config/orchestrator_pipeline_demo.json --max-iterations 1`

2. **Run full test suite**
   - `pytest -q`

3. **Generate context pack**
   - `python Scripts/prepare_context_pack.py`

4. **Analyze logs**
   - Summarize failures from `logs/actions/` and `logs/tests/`
   - Detect repeated failure patterns
   - Suggest improvements to recovery logic or config knobs

5. **Open a pull request when**
   - `projects/Self-Improve/improvements.md` changes
   - New failure patterns appear
   - Config knobs need tuning

## Safety
- Never run live clicking or UI automation.
- Only run dry-run orchestrator pipelines.
- Never modify files outside the repository.
