# Channel Profile + DOTTI Contextual Anchoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add structured channel profile fields, inject them into script and DOTTI prompt generation, validate/fix DOTTI timestamps and numbering, and fix the description language bug.

**Architecture:** Extend `channels` table with 5 profile fields; add `update_channel()` and two new API endpoints; inject profile context into script system prompt + structural variation block; add DOTTI Phase 0 anchor extraction + `_validate_and_fix_prompts()`; fix `_auto_trigger_description` language bug; add profile edit UI with AI-generation preview.

**Tech Stack:** Flask, SQLite, Anthropic Claude API, Bootstrap 5 dark-theme, vanilla JS

---

## File Map

| File | Change |
|---|---|
| `database.py` | 5 new columns in `CREATE TABLE channels`; ALTER TABLE migration loop; `update_channel()` function |
| `app.py` | `PUT /api/channels/<id>`; `POST /api/channels/<id>/generate-profile`; channel profile injection in script system_prompt; structural variation block; description language fix via `_LANG_MAP`; `_extract_dotti_anchor()`; `_validate_and_fix_prompts()`; updated `_bg_prompts`; updated `api_prompts_generate` |
| `prompts/dotti_agent.txt` | Anchor respect paragraph at start of file |
| `templates/channel_detail.html` | Channel profile edit section + "Gerar perfil com IA" button + preview modal JS |

---

## Task 1: database.py — Migration + update_channel()

**Files:**
- Modify: `H:\DOWN\Youtube\database.py:194-227` (CREATE TABLE channels)
- Modify: `H:\DOWN\Youtube\database.py:228-241` (migration loop)
- Modify: `H:\DOWN\Youtube\database.py:300-305` (after delete_channel)

- [ ] Add 5 columns to `CREATE TABLE IF NOT EXISTS channels` in `init_production_tables()`
- [ ] Add ALTER TABLE migration loop for existing DBs after existing migration block
- [ ] Add `update_channel(channel_id, **fields)` function after `delete_channel()`
- [ ] Verify `get_channel()` returns new fields automatically (it does via `SELECT *`)

---

## Task 2: app.py — Channel API endpoints

**Files:**
- Modify: `H:\DOWN\Youtube\app.py` (add after `api_production_channel` route ~line 818)

- [ ] Add `PUT /api/channels/<int:channel_id>` — accepts any subset of channel fields, calls `update_channel()`
- [ ] Add `POST /api/channels/<int:channel_id>/generate-profile` — reads up to 10 productions × 500 chars, calls Claude, returns `{profile: {...}}` (never auto-saves)

---

## Task 3: app.py — Script system_prompt injection

**Files:**
- Modify: `H:\DOWN\Youtube\app.py:730-759` (api_script_generate system_prompt)

- [ ] Read `tema_principal`, `subtema`, `tipo_canal`, `instrucoes_roteiro` from channel
- [ ] Append PERFIL DO CANAL block to system_prompt if any field non-empty (omit blank lines)
- [ ] Append VARIAÇÃO ESTRUTURAL OBRIGATÓRIA block (always, 7 opening styles)

---

## Task 4: app.py — Description language bug fix

**Files:**
- Modify: `H:\DOWN\Youtube\app.py:1039-1043`

- [ ] Replace inline dict with `_LANG_MAP.get(lang_code, "portuguese").capitalize()`
- [ ] Change default `"it"` to `"pt"` for sensible fallback

---

## Task 5: app.py — DOTTI Phase 0 + validate/fix

**Files:**
- Modify: `H:\DOWN\Youtube\app.py` (add `_extract_dotti_anchor()` and `_validate_and_fix_prompts()` before `_bg_prompts`)
- Modify: `H:\DOWN\Youtube\app.py:1268-1287` (`_bg_prompts`)
- Modify: `H:\DOWN\Youtube\app.py:1355-1385` (`api_prompts_generate`)

- [ ] Add `_extract_dotti_anchor(script_text)` — calls Claude, returns `{periodo, localizacao, restricoes}` or None
- [ ] Add `_validate_and_fix_prompts(text)` — fixes sequential numbering gaps/duplicates and timestamp overlaps
- [ ] Update `_bg_prompts(job_id, prod_id, user_msg, script_text="", instrucoes_visuais="")` — Phase 0, anchor prepend, validate after generation
- [ ] Update `api_prompts_generate` — pass `script_text` and `instrucoes_visuais` to thread

---

## Task 6: prompts/dotti_agent.txt — Anchor paragraph

**Files:**
- Modify: `H:\DOWN\Youtube\prompts\dotti_agent.txt:1-10`

- [ ] Add anchor respect paragraph after the ASCII header block, before `## 🎬 CONCEITO`

---

## Task 7: channel_detail.html — Channel profile UI

**Files:**
- Modify: `H:\DOWN\Youtube\templates\channel_detail.html:166-178` (channel hero)
- Modify: `H:\DOWN\Youtube\templates\channel_detail.html` (add modal + JS)

- [ ] Add "⚙ Perfil do Canal" button in channel hero
- [ ] Add profile edit modal with 5 fields + name + description
- [ ] Add "Gerar perfil com IA" button inside modal that calls generate-profile endpoint
- [ ] Show AI-suggested preview (editable) before saving
- [ ] "Salvar" calls `PUT /api/channels/<id>` with form data
