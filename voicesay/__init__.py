from .voicesay import VoiceSay


async def setup(bot):
    await bot.add_cog(VoiceSay(bot))
