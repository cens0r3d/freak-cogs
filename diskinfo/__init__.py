from .diskinfo import DiskInfo


async def setup(bot):
    await bot.add_cog(DiskInfo(bot))
