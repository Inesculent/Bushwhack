# Community 11: Text Cleaning Utilities

**Purpose:** This community provides text normalization and cleaning functions for multilingual and basic text inputs. These utilities convert numbers, currency, and abbreviations into their word equivalents and handle punctuation cleanup, which is essential for preprocessing text before passing to text encoders in the ComfyUI pipeline. The cleaned text ensures consistent formatting for downstream speech-to-text or natural language processing components.

## Files
- `comfy/text_encoders/ace_text_cleaners.py`: Contains helper functions for multilingual text normalization, including number expansion, currency handling, and punctuation cleanup. These functions are used by external processes that ingest raw text and require standardized output for language models. (confidence 0.90)

## Symbols
- `0e9796e65cff5e40`: Expands symbols (e.g., ampersands, asterisks) into their full written form based on language context. Critical for maintaining semantic integrity when processing text with special characters. (confidence 0.80)
  - _Rationale:_ Function name and signature suggest processing text with special symbols.

## Cross-community dependencies
0, 3

## Unverified / resolved calls
- unresolved: `CaseConverter` from `unknown` — Might be applied to adjust text casing after cleaning. Verification needed.
- unresolved: `Publisher` from `unknown` — Could be used for event publishing after text transformation. Verification required.
- unresolved: `VoiceBpeTokenizer` from `unknown` — Likely invoked after text cleaning for tokenization. Behavior needs verification in tokenizer module.
