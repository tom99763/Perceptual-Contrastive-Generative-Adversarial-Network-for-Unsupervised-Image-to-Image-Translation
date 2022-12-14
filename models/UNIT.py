import sys

sys.path.append('./models')
from modules import *
from losses import *
from discriminators import Discriminator
import tensorflow as tf


class Encoder(tf.keras.Model):
    def __init__(self, config):
        super().__init__()
        self.act = config['act']
        self.use_bias = config['use_bias']
        self.norm = config['norm']
        self.num_downsampls = config['num_downsamples']
        self.num_resblocks = config['num_resblocks']
        dim = config['base']

        self.blocks = tf.keras.Sequential([
            Padding2D(3, pad_type='reflect'),
            ConvBlock(dim, 7, padding='valid', use_bias=self.use_bias, norm_layer=self.norm, activation=self.act),
        ])
        for _ in range(self.num_downsampls):
            dim = dim * 2
            self.blocks.add(ConvBlock(dim, 3, strides=2, padding='same', use_bias=self.use_bias, norm_layer=self.norm,
                                      activation=self.act))

        for _ in range(self.num_resblocks):
            self.blocks.add(ResBlock(dim, 3, self.use_bias, self.norm))

    def call(self, x):
        return self.blocks(x)


class Decoder(tf.keras.Model):
    def __init__(self, config, opt):
        super().__init__()
        self.act = config['act']
        self.use_bias = config['use_bias']
        self.norm = config['norm']
        self.num_downsampls = config['num_downsamples']
        self.num_resblocks = config['num_resblocks']
        dim = config['base']
        num_channels = opt.num_channels

        self.blocks = tf.keras.Sequential()

        for _ in range(self.num_resblocks):
            self.blocks.add(ResBlock(dim, 3, self.use_bias, self.norm))

        for _ in range(self.num_downsampls):
            dim = dim / 2
            self.blocks.add(ConvTransposeBlock(dim, 3, strides=2, padding='same',
                                               use_bias=self.use_bias, norm_layer=self.norm, activation=self.act))
        self.blocks.add(Padding2D(3, pad_type='reflect'))
        self.blocks.add(ConvBlock(3, 7, padding='valid', activation='tanh'))

    def call(self, x):
        return self.blocks(x)


class Generator(tf.keras.Model):
    def __init__(self, config, opt):
        super().__init__()
        self.E = Encoder(config)
        self.D = Decoder(config, opt)

    def call(self, x, training=False):
        h, z = self.encode(x)
        if training:
            x = self.decode(h + z)
        else:
            x = self.D(h)
        return x

    def encode(self, x):
        h = self.E(x)
        z = tf.random.normal(h.shape)
        return h, z

    def decode(self, x):
        return self.D(x)


class UNIT(tf.keras.Model):
    def __init__(self, config, opt):
        super().__init__()
        self.Ga = Generator(config, opt)
        self.Gb = Generator(config, opt)
        self.Da = Discriminator(config)
        self.Db = Discriminator(config)
        self.style_dim = config['style_dim']
        self.config = config

    def compile(self,
                Ga_optimizer,
                Gb_optimizer,
                Da_optimizer,
                Db_optimizer
                ):
        super().compile()
        self.Ga_optimizer = Ga_optimizer
        self.Gb_optimizer = Gb_optimizer
        self.Da_optimizer = Da_optimizer
        self.Db_optimizer = Db_optimizer

    @tf.function
    def train_step(self, inputs):
        xa, xb = inputs

        with tf.GradientTape(persistent=True) as tape:
            za = tf.random.normal((xa.shape[0], 1, 1, self.style_dim))
            zb = tf.random.normal((xb.shape[0], 1, 1, self.style_dim))

            ### forward
            # encode
            ha, za_prime = self.Ga.encode(xa)
            hb, zb_prime = self.Gb.encode(xb)

            # within domain
            xar = self.Ga.decode(ha + za_prime)
            xbr = self.Gb.decode(hb + zb_prime)

            # cross domain
            xba = self.Ga.decode(hb + za)
            xab = self.Gb.decode(ha + zb)

            # cyclic encode
            hba, zba = self.Ga.encode(xba)
            hab, zab = self.Gb.encode(xab)

            # cyclic decode
            xaba = self.Ga.decode(hab + za_prime)
            xbab = self.Gb.decode(hba + zb_prime)

            # discrimination
            critic_real_a = self.Da(xa)
            critic_real_b = self.Db(xb)
            critic_fake_a = self.Da(xba)
            critic_fake_b = self.Db(xab)

            ### compute loss
            # reconstruction
            l_ra = l1_loss(xa, xar)
            l_rb = l1_loss(xb, xbr)

            # latent reconstruction
            l_za = l1_loss(za, zba)
            l_zb = l1_loss(zb, zab)
            l_z = l_za + l_zb

            # content reconstruction
            l_ha = l1_loss(ha, hab)
            l_hb = l1_loss(hb, hba)
            l_h = l_ha + l_hb

            # kl-div
            l_kl_a = l_kl(ha) + l_kl(hab)
            l_kl_b = l_kl(hb) + l_kl(hba)
            kl_loss =l_kl_a + l_kl_b

            # cyclic
            l_cycle = l1_loss(xa, xaba) + l1_loss(xb, xbab)

            # adversarial loss
            d_loss_a, g_loss_a = gan_loss(critic_real_a, critic_fake_a, self.config['gan_mode'])
            d_loss_b, g_loss_b = gan_loss(critic_real_b, critic_fake_b, self.config['gan_mode'])

            l_ga = 10 * l_ra + l_z + l_h + 0.01 * kl_loss + 10 * l_cycle + 1 * g_loss_a
            l_gb = 10 * l_rb + l_z + l_h + 0.01 * kl_loss + 10 * l_cycle + 1 * g_loss_b
            l_da = 1 * d_loss_a
            l_db = 1 * d_loss_b

        Gagrads = tape.gradient(l_ga, self.Ga.trainable_weights)
        Gbgrads = tape.gradient(l_gb, self.Gb.trainable_weights)
        Dagrads = tape.gradient(l_da, self.Da.trainable_weights)
        Dbgrads = tape.gradient(l_db, self.Db.trainable_weights)

        self.Ga_optimizer.apply_gradients(zip(Gagrads, self.Ga.trainable_weights))
        self.Gb_optimizer.apply_gradients(zip(Gbgrads, self.Gb.trainable_weights))
        self.Da_optimizer.apply_gradients(zip(Dagrads, self.Da.trainable_weights))
        self.Db_optimizer.apply_gradients(zip(Dbgrads, self.Db.trainable_weights))

        return {'l_kl': 0.5 * (l_kl_a + l_kl_b), 'l_r': 0.5 * (l_ra + l_rb), 'l_z': 0.5 * (l_za + l_zb),
                'l_h': 0.5 * (l_ha + l_hb), 'l_cycle': l_cycle,
                'g_loss': 0.5 * (g_loss_a + g_loss_b), 'd_loss': 0.5 * (d_loss_a + d_loss_b)}

    @tf.function
    def test_step(self, inputs):
        xa, xb = inputs
        za = tf.random.normal((xa.shape[0], 1, 1, self.style_dim))
        zb = tf.random.normal((xb.shape[0], 1, 1, self.style_dim))

        ### forward
        # encode
        ha, za_prime = self.Ga.encode(xa)
        hb, zb_prime = self.Gb.encode(xb)

        # within domain
        xar = self.Ga.decode(ha + za_prime)
        xbr = self.Gb.decode(hb + zb_prime)

        # cross domain
        xba = self.Ga.decode(hb + za)
        xab = self.Gb.decode(ha + zb)

        # cyclic encode
        hba, zba = self.Ga.encode(xba)
        hab, zab = self.Gb.encode(xab)

        # cyclic decode
        xaba = self.Ga.decode(hab + za_prime)
        xbab = self.Gb.decode(hba + zb_prime)

        ### compute loss
        # reconstruction
        l_ra = l1_loss(xa, xar)
        l_rb = l1_loss(xb, xbr)

        # latent reconstruction
        l_za = l1_loss(za, zba)
        l_zb = l1_loss(zb, zab)

        # content reconstruction
        l_ha = l1_loss(ha, hab)
        l_hb = l1_loss(hb, hba)

        # kl-div
        l_kl_a = l_kl(ha) + l_kl(hab)
        l_kl_b = l_kl(hb) + l_kl(hba)

        # cyclic
        l_cycle = l1_loss(xa, xaba) + l1_loss(xb, xbab)

        return {'l_kl': 0.5 * (l_kl_a + l_kl_b), 'l_r': 0.5 * (l_ra + l_rb), 'l_z': 0.5 * (l_za + l_zb),
                'l_h': 0.5 * (l_ha + l_hb), 'l_cycle': l_cycle}
