Fixed
`tares factory report grep --days` no longer crashes: a superscript or other non-ASCII digit is refused with the usual message, and a huge number reads as every day instead of failing with an OverflowError.
