# namint

OSINT username enumeration, powered by
[Naminter](https://github.com/3xp0rt/Naminter) and the
[WhatsMyName](https://github.com/WebBreacher/WhatsMyName) dataset.

| Command | What it does |
| --- | --- |
| `[p]naminter check <username>` | Check the username against every known site (aliases: `lookup`, `scan`, `enumerate`) |
| `[p]naminter sites [query]` | The loaded site list, filterable |
| `[p]naminter categories` | Sites grouped by category |
| `[p]naminter stats` | Dataset and run statistics |
| `[p]naminter refresh` | Re-fetch the dataset (**Manage Server**) |

Needs Python 3.11+ and pulls extra packages on `[p]cog install`, so the install
step takes noticeably longer than for the other cogs.

The folder is called `namint`, not `naminter`, on purpose: Red puts the cogs
directory on `sys.path`, so a folder named `naminter` would shadow the package
the cog imports. The cog and its commands still use the name **Naminter**.
