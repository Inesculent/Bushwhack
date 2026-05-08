# Community 12: Text Cleaning Functions

**Purpose:** Provide various text cleaning and expansion functions for different languages and use cases.

## Files
- `comfy/text_encoders/ace_text_cleaners.py`: Define functions to clean and expand text for multilingual support, including handling numbers, currencies, and abbreviations. (confidence 1.00)

## Symbols
- `0e9796e65cff5e40`: Expand symbols in the text based on the language specified. (confidence 1.00)
  - _Rationale:_ The function name and parameters suggest it is designed to handle symbol expansion for multilingual text processing.
- `13c0fad98207ad8f`: Convert the entire text to lowercase. (confidence 1.00)
  - _Rationale:_ The function name 'lowercase' directly indicates its purpose.
- `1f739218ab644e18`: Expand currency symbols in the text based on the language and currency type. (confidence 1.00)
  - _Rationale:_ The function name '_expand_currency' suggests it handles currency symbol expansion.
- `2c316cf666692c99`: Expand number symbols in the text based on the language. (confidence 1.00)
  - _Rationale:_ The function name '_expand_number' suggests it handles number symbol expansion.
- `3bd86a68de0d8eb6`: Apply multiple cleaning steps to the text for multilingual support. (confidence 1.00)
  - _Rationale:_ The function name 'multilingual_cleaners' indicates it applies various cleaning steps for multilingual text.
- `463abe05d7f20d3c`: Convert a number to its textual representation, optionally as an ordinal. (confidence 1.00)
  - _Rationale:_ The function name 'number_to_text' and parameter 'ordinal' suggest its purpose.
- `49279eaa82196642`: Apply basic cleaning steps to the text. (confidence 1.00)
  - _Rationale:_ The function name 'basic_cleaners' indicates it applies basic text cleaning.
- `4c2c14d6d3c94f73`: Convert a single digit to its textual representation. (confidence 1.00)
  - _Rationale:_ The function name '_digit_to_text' suggests its purpose.
- `579bf48c781b8114`: Remove commas from the text. (confidence 1.00)
  - _Rationale:_ The function name '_remove_commas' suggests its purpose.
- `764fd75a57d95d56`: Expand numbers in the text based on the language specified. (confidence 1.00)
  - _Rationale:_ The function name 'expand_numbers_multilingual' suggests it handles number expansion for multilingual text.
- `7b57c81d7cc97afd`: Remove dots from the text. (confidence 1.00)
  - _Rationale:_ The function name '_remove_dots' suggests its purpose.
- `90a622cdacb57870`: Collapse multiple whitespaces into a single space. (confidence 1.00)
  - _Rationale:_ The function name 'collapse_whitespace' suggests its purpose.
- `a3cda749d4aef285`: Expand decimal points in the text based on the language. (confidence 1.00)
  - _Rationale:_ The function name '_expand_decimal_point' suggests its purpose.
- `b2664514ff327a2d`: Expand ordinal numbers in the text based on the language. (confidence 1.00)
  - _Rationale:_ The function name '_expand_ordinal' suggests its purpose.
- `b3ada64fb931171c`: Expand abbreviations in the text based on the language specified. (confidence 1.00)
  - _Rationale:_ The function name 'expand_abbreviations_multilingual' suggests it handles abbreviation expansion for multilingual text.
- `b9e95dd8353658c8`: Convert an integer to its textual representation. (confidence 1.00)
  - _Rationale:_ The function name '_int_to_text' suggests its purpose.

## Cross-community dependencies
2, 3, 4

## Unverified / resolved calls
- unresolved: `CaseConverter` from `UnverifiedCallSource` — Cross-community callee name without body
- unresolved: `Publisher` from `UnverifiedCallSource` — Cross-community callee name without body
- unresolved: `VoiceBpeTokenizer` from `UnverifiedCallSource` — Cross-community callee name without body
