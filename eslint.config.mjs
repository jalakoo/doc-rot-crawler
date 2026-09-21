import js from "@eslint/js";
import globals from "globals";

export default [
  js.configs.recommended,
  {
    files: ["docrot/ui/**/*.js"],
    languageOptions: {
      ecmaVersion: 2020,
      sourceType: "script",
      globals: { ...globals.browser, module: "readonly" },
    },
    rules: {
      "no-var": "off",                 // plain ES5-style scripts, no build step
      eqeqeq: ["error", "smart"],
      "no-implicit-globals": "error",
      "no-unused-vars": ["error", { args: "none" }],
    },
  },
  {
    files: ["tests/js/**/*.mjs"],
    languageOptions: { ecmaVersion: 2023, sourceType: "module", globals: globals.node },
  },
];
