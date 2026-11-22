# Self-Learning deployment

`C:\Users\Administrator\Documents\GitHub\astrbot_plugin_self_learning` is the source checkout.
AstrBot loads the generated runtime copy from
`C:\Users\Administrator\Downloads\AstrBot\data\plugins\astrbot_plugin_self_learning`.

Configure the local commit hook once from PowerShell:

```powershell
.\setup_git_hooks.ps1
```

After a successful commit, the hook deploys that exact commit with
`deploy_self_learning.ps1 -SourceRef HEAD`. It preserves the current runtime-only
`pages`, `web_src`, `docs` and `.astrbot-plugin` resources until those resources are
imported into this repository. It also preserves unrelated runtime files when the
hook uses `-PreserveExtraFiles`.

Run a manual deployment with:

```powershell
.\deploy_self_learning.ps1 -PreserveExtraFiles
```

Use `-DryRun` to inspect file operations before applying them. Restart or reload
AstrBot after deployment so the plugin process loads the updated files.
