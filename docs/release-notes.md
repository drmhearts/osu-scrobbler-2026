# osu! life hacks 0.9.6

- Firefox beatmap downloads now use a small add-on in the user's regular
  Firefox profile. They use the browser's normal session without copying
  cookies or starting an automated Firefox window.
- Pair once from maps → p. The add-on sends completed archive paths to the app,
  which validates and imports copies using the existing queue and cooldowns.
- Removed Selenium and geckodriver from the app runtime. Chrome and Edge keep
  their existing download workflow.
- Firefox requires Mozilla signing for permanent installation. This release
  includes a temporary add-on that must be reloaded after Firefox restarts.

©Hky Sigh, 2026
