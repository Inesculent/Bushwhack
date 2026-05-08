# Community 13: Text Cleaning

**Purpose:** Provides functions to clean and preprocess text data for various languages.

## Files
- `comfy/text_encoders/ace_text_cleaners.py`: Contains utility functions for cleaning and expanding text elements like numbers, currencies, and abbreviations in multiple languages. (confidence 1.00)

## Symbols
- `symbol:0e9796e65cff5e40`: Expands symbols in text based on language. (confidence 1.00)
  - _Rationale:_ The function takes text and an optional language parameter, suggesting it processes text for different linguistic contexts.
- `symbol:13c0fad98207ad8f`: Converts all characters in the input text to lowercase. (confidence 1.00)
  - _Rationale:_ The function name 'lowercase' directly indicates its purpose.
- `symbol:1f739218ab644e18`: Expands currency symbols in text based on language and currency type. (confidence 1.00)
  - _Rationale:_ The function name '_expand_currency' suggests it handles currency symbols, and parameters indicate language and currency type.
- `symbol:2c316cf666692c99`: Expands number representations in text based on language. (confidence 1.00)
  - _Rationale:_ The function name '_expand_number' suggests it handles numeric values, and the language parameter indicates linguistic context.
- `symbol:3bd86a68de0d8eb6`: Applies multiple cleaning functions to text based on language. (confidence 1.00)
  - _Rationale:_ The function name 'multilingual_cleaners' indicates it applies various cleaning operations, and the language parameter suggests it works with multiple languages.
- `symbol:463abe05d7f20d3c`: Converts numbers to their textual representation. (confidence 1.00)
  - _Rationale:_ The function name 'number_to_text' directly indicates its purpose, and the ordinal parameter suggests it can handle both regular and ordinal numbers.
- `symbol:49279eaa82196642`: Applies basic cleaning functions to text. (confidence 1.00)
  - _Rationale:_ The function name 'basic_cleaners' indicates it performs fundamental text cleaning tasks.
- `symbol:4c2c14d6d3c94f73`: Converts single digit numbers to their textual representation. (confidence 1.00)
  - _Rationale:_ The function name '_digit_to_text' suggests it converts individual digits to words.
- `symbol:579bf48c781b8114`: Removes commas from text using a match object. (confidence 1.00)
  - _Rationale:_ The function name '_remove_commas' indicates its purpose, and the match object parameter suggests it is used in a regular expression context.
- `symbol:764fd75a57d95d56`: Expands number representations in text based on language. (confidence 1.00)
  - _Rationale:_ The function name 'expand_numbers_multilingual' suggests it handles numeric values, and the language parameter indicates linguistic context.
- `symbol:7b57c81d7cc97afd`: Removes dots from text using a match object. (confidence 1.00)
  - _Rationale:_ The function name '_remove_dots' indicates its purpose, and the match object parameter suggests it is used in a regular expression context.
- `symbol:90a622cdacb57870`: Collapses multiple whitespace characters into a single space. (confidence 1.00)
  - _Rationale:_ The function name 'collapse_whitespace' directly indicates its purpose.
- `symbol:a3cda749d4aef285`: Expands decimal points in text based on language. (confidence 1.00)
  - _Rationale:_ The function name '_expand_decimal_point' suggests it handles decimal points, and the language parameter indicates linguistic context.
- `symbol:b2664514ff327a2d`: Expands ordinal numbers in text based on language. (confidence 1.00)
  - _Rationale:_ The function name '_expand_ordinal' suggests it handles ordinal numbers, and the language parameter indicates linguistic context.
- `symbol:b3ada64fb931171c`: Expands abbreviations in text based on language. (confidence 1.00)
  - _Rationale:_ The function name 'expand_abbreviations_multilingual' suggests it handles abbreviations, and the language parameter indicates linguistic context.
- `symbol:b9e95dd8353658c8`: Converts integers to their textual representation. (confidence 1.00)
  - _Rationale:_ The function name '_int_to_text' suggests it converts integer values to words.

## Cross-community dependencies
1, 3, 4

## Unverified / resolved calls
- unresolved: `CaseConverter` from `UnverifiedCallTarget` — Not visible in provided context.
- unresolved: `Publisher` from `UnverifiedCallTarget` — Not visible in provided context.
- unresolved: `VoiceBpeTokenizer` from `UnverifiedCallTarget` — Not visible in provided context.
