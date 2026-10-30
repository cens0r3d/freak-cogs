from .shazam import Shazam

__all__ = ["Shazam"]


async def setup(bot):
    await bot.add_cog(Shazam(bot))
