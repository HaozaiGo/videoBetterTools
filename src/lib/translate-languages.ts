export const translateTargetLanguages = [
  { value: "en", label: "英文", promptName: "English" },
  { value: "ja", label: "日文", promptName: "Japanese" },
  { value: "ko", label: "韩文", promptName: "Korean" },
  { value: "es", label: "西班牙语", promptName: "Spanish" },
  { value: "fr", label: "法语", promptName: "French" },
  { value: "de", label: "德语", promptName: "German" },
  { value: "pt", label: "葡萄牙语", promptName: "Portuguese" },
  { value: "ru", label: "俄语", promptName: "Russian" },
  { value: "it", label: "意大利语", promptName: "Italian" },
  { value: "vi", label: "越南语", promptName: "Vietnamese" },
  { value: "th", label: "泰语", promptName: "Thai" },
  { value: "id", label: "印尼语", promptName: "Indonesian" },
  { value: "ar", label: "阿拉伯语", promptName: "Arabic" },
  { value: "hi", label: "印地语", promptName: "Hindi" },
] as const;

export type TranslateTargetLanguage = (typeof translateTargetLanguages)[number]["value"];

export function translateLanguageLabel(value: string) {
  return translateTargetLanguages.find((language) => language.value === value)?.label || value;
}
