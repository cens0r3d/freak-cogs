from .bandcamp import Bandcamp


async def setup(bot):
    await bot.add_cog(Bandcamp(bot))
