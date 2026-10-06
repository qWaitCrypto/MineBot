package dev.minebot.client.body.mixin;

import net.minecraft.client.multiplayer.resolver.AddressCheck;
import net.minecraft.client.multiplayer.resolver.ServerAddress;
import net.minecraft.client.multiplayer.resolver.ServerNameResolver;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Redirect;

/**
 * Keeps the local Phase A harness independent of Mojang's external address
 * blocklist service. This is client-only test infrastructure, not a product
 * networking policy.
 */
@Mixin(ServerNameResolver.class)
public abstract class ServerNameResolverMixin {
    @Redirect(
        method = "<clinit>",
        at = @At(
            value = "INVOKE",
            target = "Lnet/minecraft/client/multiplayer/resolver/AddressCheck;createFromService()Lnet/minecraft/client/multiplayer/resolver/AddressCheck;"
        )
    )
    private static AddressCheck minebot$allowLocalAddresses() {
        return new AddressCheck() {
            @Override
            public boolean isAllowed(net.minecraft.client.multiplayer.resolver.ResolvedServerAddress address) {
                return true;
            }

            @Override
            public boolean isAllowed(ServerAddress address) {
                return true;
            }
        };
    }
}
