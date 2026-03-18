# Proton 10 Experimental Bleeding Edge with Hoshino Lina's Patch
## Custom patched Proton for the Unity extreme memory leak that occurs with some version of mono's garbage collector

This is a fork of [Wine-TKG-Git](https://github.com/Frogging-Family/wine-tkg-git) that includes a patch by Hoshino Lina that fix's/worksaround a bug in Wine that can lead to an extreme memory leak in some version's of mono's garbage collector which is used in some versions of Unity.

Specifically, this is intended to fix things for Warudo and other software that uses those specific versions of Unity/Mono GC.

This also includes some specific workarounds for Warudo to make launching it as simple as possible.

## Notes

The ideal solution would be a fix applied directly to Wine and adopted in to Valve Wine, and for software using that Unity/Mono GC library to upgrade to a newer version of the GC library that handles these exceptions without the leak.

Unfortunately it can takes months or more to get a fix like this in to the Wine source and then in to Valve's Wine source, and then in to a version of Proton that is easily accessible for users.

This works as a stop-gap solution until all of that happens.

## Usage

1. **Download the correct release for your situation:**
- [Valve Proton v3](https://github.com/madalee-com/wine-tkg-git-lina-unity-leak-fix/releases/tag/v3-tkg-valve-rev5) if you need to use Spout2pw or want a release closer to Valve's Steam Proton that might be more compatible with other games.
- [TKG Proton v3](https://github.com/madalee-com/wine-tkg-git-lina-unity-leak-fix/releases/tag/v3-tkg-valve-nr) if you want the most similar to the Wine-TKG-Git implementation and you do not need to use Spout2pw.

2. **Extract the zip to the compatability tools path:**
- For directly installed Wine: `~/.local/share/Steam/compatibilitytools.d/`
- For Flatpack Wine: `~/.var/app/com.valvesoftware.Steam/.local/share/Steam/compatibilitytools.d/`

3. **Restart Steam if it's already running**

### If you are trying to use Spout2PW
You may get an error when starting Warudo with spout2pw saying **"Installation unsuccessful"**.
If this happens:
- Switch to Proton Experimental
- Try to start Warudo(it's okay if it crashes, just so long as Spout gets setup and says **"Installation successful"**).
- Then switch back to this patched Proton and everything should work.

## Acknowledgements

- 99.9% of this project is: https://github.com/Frogging-Family/wine-tkg-git
- [Hoshino Lina](https://github.com/hoshinolina) for making the patch
- [AdalynBlack](https://github.com/AdalynBlack) for helping to isolate the actual bug
- [Madalee](https://github.com/madalee-com) for putting this fork together

