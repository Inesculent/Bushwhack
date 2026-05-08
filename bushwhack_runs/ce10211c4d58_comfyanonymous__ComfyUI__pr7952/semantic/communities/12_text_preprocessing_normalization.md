# Community 12: Text Preprocessing & Normalization

**Purpose:** This community provides utility functions for cleaning, expanding, and normalizing text across multiple languages, preparing raw input for downstream text processing pipelines. It appears to handle tasks like number expansion, symbol expansion, currency handling, and punctuation cleanup, primarily supporting multilingual text normalization workflows in the repository.

## Files
- `comfy/text_encoders/ace_text_cleaners.py`: Main module containing multilingual text cleaning functions, number-to-text conversion, and normalization utilities. Likely used to preprocess text before encoding, especially for models requiring expanded or standardized numeric/symbolic representation. (confidence 0.90)

## Symbols
- `0e9796e65cff5e40`: Expands text symbols (e.g., & to 'and') for multilingual input, likely used before tokenization. (confidence 0.95)
  - _Rationale:_ Function name and parameters suggest handling of symbol normalization across languages.
- `13c0fad98207ad8f`: Converts text to lowercase, a common preprocessing step. (confidence 0.95)
  - _Rationale:_ Standard text normalization function with no special language parameters beyond 'text'.
- `1f739218ab644e18`: Expands currency symbols (e.g., $, €) to their written form in specified language and currency. (confidence 0.95)
  - _Rationale:_ Parameters include currency type and language, indicating multilingual currency expansion.
- `2c316cf66692c99`: Expands numeric values (e.g., 10 to 'ten') in text for spoken or normalized form. (confidence 0.95)
  - _Rationale:_ Named '_expand_number' with match object 'm', likely regex-based expansion of numeric literals.
- `3bd86a68de0d8eb6`: Orchestrates multilingual text cleaning by combining multiple cleaning functions for specified language. (confidence 0.95)
  - _Rationale:_ Function takes 'text' and 'lang' and likely applies sequence of cleaners defined in the module.
- `463abe05d7f20d3c`: Converts a numeric value to its textual representation (e.g., 1 to 'one', optionally ordinal). (confidence 0.95)
  - _Rationale:_ Core number-to-text conversion utility with optional ordinal flag.
- `49279eaa82196642`: Applies basic text cleaning rules (likely punctuation removal, case normalization, whitespace collapse). (confidence 0.95)
  - _Rationale:_ Named 'basic_cleaners', likely a composition of simpler text normalization steps.
- `4c2c14d6d3c94f73`: Converts single digits to text (e.g., 5 to 'five'), building block for number expansion. (confidence 0.95)
  - _Rationale:_ Private function '_digit_to_text' likely used internally by number expansion functions.
- `579bf48c781b8114`: Removes commas from numeric strings, possibly to standardize number formatting. (confidence 0.95)
  - _Rationale:_ Regex-based matcher 'm' suggests handling of commas in numeric contexts.
- `764fd75a57d95d56`: Expands numeric phrases and digits within text for multilingual support. (confidence 0.95)
  - _Rationale:_ Multilingual variant of number expansion, taking language parameter.
- `7b57c81d7cc97afd`: Removes dots (periods) from text, possibly in numeric or abbreviations context. (confidence 0.95)
  - _Rationale:_ Regex matcher 'm' indicates localized text transformation, likely for numbers.
- `90a622cdacb57870`: Collapses consecutive whitespace characters into single spaces. (confidence 0.95)
  - _Rationale:_ Standard text normalization step for cleaning up spacing artifacts.
- `a3cda749aef285`: Expands decimal points in numbers to spoken form (e.g., '3.14' to 'three point one four'). (confidence 0.95)
  - _Rationale:_ Named '_expand_decimal_point' with regex matcher and language parameter.
- `b2664514f365c8`: Converts ordinal numbers (e.g., '1st') to textual form ('first') or expands them. (confidence 0.95)
  - _Rationale:_ Private function for ordinal handling, part of number normalization suite.
- `b3ada64fb931171c`: Expands common abbreviations in text, multilingual support. (confidence 0.95)
  - _Rationale:_ Function expands abbreviations with language-specific handling.
- `b9e95dd835365c8`: Converts integers to text words, core utility for number expansion. (confidence 0.95)
  - _Rationale:_ Private function '_int_to_text' likely used by higher-level number expansion functions.

## Cross-community dependencies
0, 3

## Unverified / resolved calls
- unresolved: `CaseConverter` from `3bd86a68de0d8eb6` — Unclear if case conversion happens before or after this module's 'lowercase' function; verify if external case handling is needed.
- unresolved: `Publisher` from `3bd86a68de0d8eb6` — Unknown publisher role in text cleaning; may be for logging, publishing results, or external integration.
- unresolved: `VoiceBpeTokenizer` from `3bd86a68de0d8eb6` — Likely uses tokenizer in text cleaning pipeline; verify if tokenizer is applied after cleaning.
