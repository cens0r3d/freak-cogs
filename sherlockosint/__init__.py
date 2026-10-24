from .sherlockosint import SherlockOsint


async def setup(bot):
    cog = SherlockOsint(bot)
    await bot.add_cog(cog)