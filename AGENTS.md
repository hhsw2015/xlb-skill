# Repository Guidelines

## Project Structure & Module Organization
This repository is currently minimal and documentation-first. At the moment, `AGENTS.md` is the primary project file.
When adding implementation files, use predictable top-level folders:
- `src/` for runtime code
- `tests/` for automated tests
- `assets/` for static resources
- `docs/` for design notes or ADRs
Keep modules small and grouped by feature (for example, `src/auth/` or `src/parser/`).

## Build, Test, and Development Commands
No build system is configured yet. Use these baseline commands from the repository root:
- `rg --files`: list project files quickly.
- `rg -n "<pattern>" .`: search code and docs.
- `markdownlint "**/*.md"`: lint documentation if `markdownlint` is installed.
If a language runtime is introduced, add project-specific scripts (such as `npm test` or `pytest`) and document them in this section.

## Coding Style & Naming Conventions
Use 2-space indentation for Markdown and YAML; for other languages, follow the formatter or linter standard for that language.
Naming conventions:
- files and directories: kebab-case (`data-loader.ts`, `api-client.py`)
- classes/types: PascalCase
- functions/variables: camelCase (JavaScript/TypeScript) or snake_case (Python)
Prefer small functions, explicit inputs/outputs, and short file-level notes when logic is not obvious.

## Testing Guidelines
Add tests in `tests/` and mirror source paths (example: `src/parser/tokenizer.ts` -> `tests/parser/tokenizer.test.ts`).
Test names should describe behavior, such as `returns_empty_list_for_blank_input`.
Before merging, cover core logic and add a regression test for each bug fix.

## Commit & Pull Request Guidelines
This workspace has no local Git history yet; adopt Conventional Commits for new work:
- `feat: add tokenizer for link metadata`
- `fix: handle empty URL input`
Pull requests should include a concise summary, clear scope, test evidence (command plus result), and screenshots only for UI changes.
