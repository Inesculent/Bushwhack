# Community 14: Multilingual Text Cleaning

**Purpose:** This community provides text preprocessing utilities for normalizing and cleaning textual input, particularly focusing on multilingual support and numerical expansions. It appears to integrate with text encoder pipelines by ensuring consistent formatting before tokenization or embedding, supporting various languages and number representations.

## Files
- `comfy/text_encoders/ace_text_cleaners.py`: Contains functions for cleaning and normalizing text in various languages, including expansion of numbers, symbols, and currency, as well as whitespace and abbreviation handling. These utilities are designed to prepare raw text for downstream processing in multilingual text encoders. (confidence 0.70)

## Symbols
- `0e9796e65cff5e40`: Expands symbols in multilingual text context, likely converting special characters into textual equivalents for better compatibility with tokenizers. (confidence 0.80)
  - _Rationale:_ Function name and signature suggest it processes text for language-specific symbol normalization.
- `13c0fad98207ad8f`: Converts text to lowercase, a standard preprocessing step to ensure uniformity. (confidence 0.90)
  - _Rationale:_ Directly inferred from function name and typical usage in text cleaning pipelines.
- `1f739218ab644e18`: Expands currency symbols and values into spoken form (e.g., '$100' → '100 dollars'). (confidence 0.85)
  - _Rationale:_ Parameter 'currency' and function name indicate it handles monetary value normalization.
- `2c316cf66692c99`: Processes matched text (likely numbers) and converts them to textual representations. (confidence 0.80)
  - _Rationale:_ Used in number expansion logic; parameter 'm' suggests it works with regex match objects.
- `3bd86a68de0d8eb6`: Main entry point for multilingual text cleaning, orchestrating multiple expansion and normalization steps. (confidence 0.90)
  - _Rationale:_ Name 'multilingual_cleaners' and 'lang' parameter indicate high-level text normalization across languages.
- `463abe05d7f20d3c`: Converts numeric values to their text representation, optionally as ordinals. (confidence 0.90)
  - _Rationale:_ Functionality is explicitly indicated by the function name and parameters.
- `49279eaa82196642`: Performs basic text cleaning operations such as normalization or formatting. (confidence 0.75)
  - _Rationale:_ General purpose cleaner; likely a prerequisite step in multilingual pipelines.
- `4c2c14d6d3c94f73`: Converts a digit character to its textual word (e.g., '5' → 'five'). (confidence 0.85)
  - _Rationale:_ Function name and parameter 'digit' indicate low-level number to text conversion.
- `579bf48c781b8114`: Removes commas from matched text, likely for numeric string normalization. (confidence 0.80)
  - _Rationale:_ Function name suggests it targets comma removal for numeric formatting.
- `764fd75a57d95d56`: Expands numeric expressions in multilingual text (e.g., '1st', 'two hundred'). (confidence 0.85)
  - _Rationale:_ Name and context suggest it handles number normalization across languages.
- `7b57c81d7cc97afd`: Removes dots (periods) from text, possibly to normalize decimal or abbreviation formats. (confidence 0.80)
  - _Rationale:_ Function name indicates it targets dot removal, commonly used in number/abbreviation cleaning.
- `90a622cdacb57870`: Collapses multiple whitespace characters into a single space. (confidence 0.95)
  - _Rationale:_ Standard text normalization function; name and purpose are unambiguous.
- `a3cda749d322c328`: Expands decimal point notation into spoken form (e.g., '3.14' → 'three point one four'). (confidence 0.85)
  - _Rationale:_ Function name indicates it handles decimal normalization.
- `b2664514ff327a2d`: Expands ordinal numbers into text (e.g., '1st' → 'first'). (confidence 0.85)
  - _Rationale:_ Function name and usage context suggest it handles ordinal to word conversion.
- `b3ada64fb931171c`: Expands common abbreviations into full words within multilingual text. (confidence 0.80)
  - _Rationale:_ Function name indicates it processes abbreviation expansions.
- `b9e95dd8353658c8`: Converts an integer to its textual representation (e.g., 42 → 'forty-two'). (confidence 0.90)
  - _Rationale:_ Function name and parameter 'num' indicate integer-to-text conversion.

## Cross-community dependencies
2, 3, 4

## Unverified / resolved calls
- unresolved: `CaseConverter` from `3bd86a68de0d8eb6` — Might handle case transformation logic not present in this file.
- unresolved: `Publisher` from `3bd86a68de0d8eb6` — Unknown integration point; name suggests event publishing or callback mechanism outside this scope.
- unresolved: `VoiceBpeTokenizer` from `3bd86a68de0d8eb6` — Potential integration with BPE tokenization logic; function name implies a tokenizer component not visible here.
