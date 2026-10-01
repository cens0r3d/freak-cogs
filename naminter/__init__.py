from .naminter import Naminter


async def setup(bot):
    await bot.add_cog(Naminter(bot))
