# Craft the Context - Code Completion Context Strategy

Hackathon solution for the "Craft the Context" task - building a context collection pipeline that enriches code completion points with relevant repository-wide information to maximize ChrF score across three LLM backends.

## Problem

Given a set of completion points in a codebase, compose the most informative context possible for each point. The context is fed to three models (Mellum, Codestral, Qwen2.5-Coder) which generate completions evaluated against ground truth using **ChrF** (character n-gram F-score).

The challenge is entirely in **context engineering** - the models are fixed, so the only lever is what information you provide them.

## Scoring

- **Metric:** ChrF - measures character-level n-gram similarity between generated and reference completions
- **Final score:** Average ChrF across all three models and all completion points
- **Evaluation pipeline:** format validation → context injection → model completion → ChrF vs. ground truth

[Repo with baseline kit](https://github.com/JetBrains-Research/EnsembleAI2026-starter-kit)

[New public dataset](https://drive.google.com/drive/folders/1U5WGXTqbED9vnkk2lDWJA4ETaxrut2we)
