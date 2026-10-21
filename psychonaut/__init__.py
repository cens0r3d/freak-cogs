from .psychonaut import Psychonaut


async def setup(bot):
    await bot.add_cog(Psychonaut(bot))
