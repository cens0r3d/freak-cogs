from .quoter import Quoter


async def setup(bot):
    await bot.add_cog(Quoter(bot))
