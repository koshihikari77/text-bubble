# Scene Placement Architecture

## Goal

Place dialogue text and speech bubbles in positions that are readable, avoid important visual content, and remain geometrically valid.

## Core Design

Treat scene placement as a constrained layout problem, not as a single monolithic LLM generation problem.

The system should split responsibilities as follows:

- VLM / LLM
  - image understanding
  - rough free-space interpretation
  - optional final qualitative review
- deterministic code
  - text size estimation
  - bbox computation
  - overlap detection
  - out-of-bounds detection
  - candidate generation
  - scoring and layout selection
- renderer
  - draw the final plan only

## Recommended Pipeline

### 1. Reflow first

Before placement, determine the vertical text columns for each bubble.

This is required because bubble occupancy depends on:

- column count
- max column length
- font size
- line spacing

Without reflow, bubble size is not yet known.

### 2. Size estimation

For each bubble, compute:

- `width_ratio`
- `height_ratio`
- estimated text bbox

These should be computed deterministically from the reflow result.

### 3. Image understanding

Run one image understanding pass to extract scene constraints.

Desired outputs are image-ratio regions such as:

- face regions
- person regions
- important action regions
- free-space candidate regions

This step can be done with a local VLM and/or segmentation models.

### 4. Candidate anchor generation

For each bubble, generate multiple placement candidates.

This should be deterministic.

Examples:

- corners of free-space regions
- coarse grid positions inside each free-space region
- positions that keep a minimum distance from face regions
- positions that preserve reading order

The system should not ask the LLM to invent exact coordinates from scratch if deterministic candidates can be enumerated.

### 5. Scoring

Each candidate should receive a cost based on constraints.

Typical rules:

- face overlap: invalid
- text overlap: invalid
- out-of-bounds: invalid
- overlap with important body/action area: high penalty
- reading-order inversion: penalty
- excessive distance from speaker region: penalty
- poor use of free-space priority: penalty
- visually balanced use of empty space: bonus or reduced penalty

### 6. Global layout selection

Choose the final layout from candidates.

Possible approaches:

- greedy placement in reading order
- beam search over multiple partial layouts
- small search over candidate combinations

Even a simple greedy solver with good constraints is likely to be more stable than a one-shot scene LLM.

### 7. Rendering

Render text and bubbles only after the final layout is fixed.

Rendering should not be responsible for discovering layout mistakes.

### 8. Final review

After rendering, optionally run one final VLM review.

The VLM should check mainly:

- whether an important gesture or expression is still obscured
- whether the composition feels clearly wrong
- whether the final arrangement is readable as manga dialogue

This stage should not be responsible for geometric overlap checks that deterministic code can already handle.

## Why Not Use One-Shot Scene Generation As The Main Path

One-shot placement can be useful as:

- a baseline
- a fallback
- a comparison target

But it should not be the main architecture because:

- failures are difficult to localize
- outputs are unstable as bubble count grows
- overlap and bounds are better handled deterministically

## Incremental Placement Direction

The most promising direction is:

1. one image understanding pass
2. deterministic bubble size computation
3. place one bubble at a time
4. update occupied regions after each placement
5. apply deterministic overlap checks between steps
6. run an optional final review once

This should reduce the number of expensive full-image calls while improving stability.

## Practical Role Split

### Use the VLM for

- scene understanding
- free-space interpretation
- speaker-region interpretation when necessary
- final qualitative review

### Do not use the VLM for

- text bbox calculation
- overlap checks
- out-of-bounds checks
- minimum spacing checks
- small coordinate nudging

## Current Best Next Step

Implement an incremental placement PoC with this structure:

1. compute `width_ratio` and `height_ratio` for all bubbles first
2. obtain free-space and avoid regions once
3. place one bubble at a time using precomputed sizes
4. update occupied regions deterministically
5. compare the result against one-shot placement

