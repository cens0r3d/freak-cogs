# namint

OSINT username enumeration for Red-DiscordBot, powered by
[Naminter](https://github.com/3xp0rt/Naminter) and the
[WhatsMyName](https://github.com/WebBreacher/WhatsMyName) dataset.

The folder is called `namint`, not `naminter`, on purpose: Red puts the cogs
directory on `sys.path`, so a cog folder named `naminter` shadows the
`naminter` package this cog imports. The cog and all its commands still use the
name **Naminter**.
